"""Работа с почтовыми ящиками по IMAP (только стандартная библиотека)."""
import base64
import email
import html
import imaplib
import json
import logging
import re
import socket
import ssl
import threading
import time
import urllib.request
from collections import OrderedDict
from datetime import timezone
from email import policy
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime, getaddresses
from urllib.parse import unquote, urlencode

log = logging.getLogger("oneinbox")
imaplib._MAXLINE = 10_000_000  # большие ящики Gmail присылают длинные строки

IMAP_HOSTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "yandex.ru": "imap.yandex.ru",
    "ya.ru": "imap.yandex.ru",
    "yandex.com": "imap.yandex.ru",
    "yandex.by": "imap.yandex.ru",
    "yandex.kz": "imap.yandex.ru",
    "yandex.ua": "imap.yandex.ru",
    "narod.ru": "imap.yandex.ru",
    "mail.ru": "imap.mail.ru",
    "bk.ru": "imap.mail.ru",
    "inbox.ru": "imap.mail.ru",
    "list.ru": "imap.mail.ru",
    "internet.ru": "imap.mail.ru",
}

ROLES = ("inbox", "sent", "drafts", "junk", "trash")
ROLE_FLAGS = {"\\sent": "sent", "\\drafts": "drafts", "\\junk": "junk", "\\trash": "trash"}
ROLE_NAMES = {  # если сервер не помечает папки флагами
    "sent": {"sent", "sent mail", "sent items", "sent messages", "отправленные"},
    "drafts": {"drafts", "черновики"},
    "junk": {"spam", "junk", "junk e-mail", "спам"},
    "trash": {"trash", "deleted", "deleted items", "deleted messages", "корзина", "удаленные", "удалённые"},
}
HIDDEN_FLAGS = {"\\noselect", "\\nonexistent", "\\important"}


class MailError(Exception):
    pass


# --- запасной DNS ----------------------------------------------------------
# Через некоторые VPN (например, с раздельным туннелированием) системный DNS не отдаёт адреса
# Яндекса, хотя сам сервер доступен. Тогда спрашиваем адрес у Google по
# DNS-over-HTTPS и подключаемся по IP; TLS-сертификат при этом всё равно
# проверяется по настоящему имени сервера, так что подмена адреса не пройдёт.
_doh_cache = {}  # host -> (ips, expires_at)


def resolve_doh(host):
    hit = _doh_cache.get(host)
    if hit and hit[1] > time.time():
        return hit[0]
    url = "https://dns.google/resolve?" + urlencode({"name": host, "type": "A"})
    req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
    answers = [a for a in data.get("Answer", []) if a.get("type") == 1]
    ips = [a["data"] for a in answers]
    if ips:
        ttl = max(60, min(3600, min(a.get("TTL", 300) for a in answers)))
        _doh_cache[host] = (ips, time.time() + ttl)
    return ips


class _IMAP4_SSL(imaplib.IMAP4_SSL):
    """IMAP4_SSL, который умеет подключаться к заранее известному IP."""

    def __init__(self, host, port, connect_ip=None, **kw):
        self._connect_ip = connect_ip
        super().__init__(host, port, **kw)

    def _create_socket(self, timeout):
        if not self._connect_ip:
            return super()._create_socket(timeout)
        sock = socket.create_connection((self._connect_ip, self.port), timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


def detect_host(address: str):
    return IMAP_HOSTS.get(address.rsplit("@", 1)[-1].strip().lower())


def _dh(value) -> str:
    """Декодирует заголовок вида =?UTF-8?B?...?= в нормальный текст."""
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value)))).strip()
    except Exception:
        return str(value).strip()


def _err_text(e) -> str:
    a = e.args[0] if getattr(e, "args", None) else e
    if isinstance(a, bytes):
        a = a.decode("utf-8", "replace")
    return str(a)


def _login_hint(host: str) -> str:
    if "gmail" in host:
        return ("Для Gmail нужен пароль приложения: включи двухэтапную проверку, "
                "затем myaccount.google.com/apppasswords.")
    if "yandex" in host:
        return ("Для Яндекса нужен пароль приложения (id.yandex.ru → Безопасность → "
                "Пароли приложений → Почта) и включённый IMAP в настройках почты "
                "(«Почтовые программы»).")
    return "Проверь пароль и что в ящике включён доступ по IMAP."


def _ts(date_header) -> float:
    try:
        dt = parsedate_to_datetime(str(date_header))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


# --- имена папок -----------------------------------------------------------
def utf7_decode(s: str) -> str:
    """Modified UTF-7 (RFC 3501) → обычная строка. «&BB4EQgQ,-» → «Отп…»."""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "&":
            j = s.find("-", i)
            if j == -1:
                out.append(s[i:])
                break
            if j == i + 1:
                out.append("&")
            else:
                b64 = s[i + 1:j].replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                try:
                    out.append(base64.b64decode(b64).decode("utf-16-be"))
                except Exception:
                    out.append(s[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _q(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) ?(?P<name>.*)$')


def _parse_list(data):
    out = []
    for item in data or []:
        if item is None:
            continue
        literal = None
        if isinstance(item, tuple):
            line, literal = item[0], item[1]
        else:
            line = item
        m = _LIST_RE.match(line.strip())
        if not m:
            continue
        flags = {f.lower() for f in m.group("flags").decode("ascii", "replace").split()}
        delim = m.group("delim").decode("ascii", "replace").strip('"')
        if literal is not None:
            name = literal.decode("utf-8", "replace")
        else:
            name = m.group("name").decode("utf-8", "replace").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out.append((name, flags, delim))
    return out


def folder_title(name: str, delim: str = "/") -> str:
    t = utf7_decode(name)
    for prefix in ("[Gmail]/", "[Google Mail]/"):
        if t.startswith(prefix):
            t = t[len(prefix):]
    if delim and delim != "NIL":
        t = t.replace(delim, " / ")
    return t


class Mailbox:
    """Одно постоянное IMAP-соединение на ящик, с переподключением."""

    def __init__(self, address, password, host, port=993):
        self.address = address
        self.password = password.replace(" ", "") if "gmail" in host else password
        self.host = host
        self.port = port
        self.lock = threading.Lock()
        self.imap = None
        self._folders = None
        self._cache = OrderedDict()  # (folder, uid) -> raw bytes

    # --- соединение ---------------------------------------------------
    def _open(self, connect_ip=None):
        # По умолчанию imaplib НЕ проверяет сертификат сервера — включаем проверку явно,
        # иначе подменённый сервер (кривой Wi-Fi/VPN) мог бы перехватить пароль.
        return _IMAP4_SSL(self.host, self.port, connect_ip=connect_ip, timeout=25,
                          ssl_context=ssl.create_default_context())

    def _open_via_doh(self):
        try:
            ips = resolve_doh(self.host)
        except Exception as e:
            raise MailError(f"Не удалось узнать адрес {self.host}: системный DNS не отвечает "
                            f"(часто из-за VPN), запасной DNS тоже недоступен ({e}).")
        if not ips:
            raise MailError(f"Не удалось узнать адрес {self.host}: DNS не знает такого сервера.")
        last = None
        for ip in ips:
            try:
                m = self._open(ip)
                log.info("%s: системный DNS не сработал, подключились через DoH к %s", self.host, ip)
                return m
            except ssl.SSLCertVerificationError as e:
                raise MailError(f"Сертификат {self.host} не прошёл проверку: {e.verify_message}")
            except (OSError, socket.timeout) as e:
                last = e
        raise MailError(f"Не удалось подключиться к {self.host} ({', '.join(ips)}): {last}. "
                        "Если включён VPN — добавь этот адрес в исключения.")

    def _connect(self):
        try:
            m = self._open()
        except socket.gaierror:
            m = self._open_via_doh()
        except ssl.SSLCertVerificationError as e:
            raise MailError(f"Сертификат {self.host} не прошёл проверку: {e.verify_message}. "
                            "Возможно, соединение кто-то перехватывает.")
        except (OSError, socket.timeout) as e:
            raise MailError(f"Не удалось подключиться к {self.host}: {e}")
        try:
            m.login(self.address, self.password)
        except imaplib.IMAP4.error as e:
            try:
                m.logout()
            except Exception:
                pass
            raise MailError(f"Сервер не пускает: {_err_text(e)}. {_login_hint(self.host)}")
        return m

    def _drop(self):
        if self.imap is not None:
            try:
                self.imap.shutdown()
            except Exception:
                pass
        self.imap = None

    def _with(self, fn):
        with self.lock:
            for attempt in (1, 2):
                if self.imap is None:
                    self.imap = self._connect()
                try:
                    return fn(self.imap)
                except (imaplib.IMAP4.abort, OSError, EOFError) as e:
                    self._drop()
                    if attempt == 2:
                        raise MailError(f"Соединение с {self.host} оборвалось: {e}")
                except imaplib.IMAP4.error as e:
                    raise MailError(_err_text(e))

    def check(self):
        m = self._connect()
        try:
            m.logout()
        except Exception:
            pass

    def close(self):
        with self.lock:
            if self.imap is not None:
                try:
                    self.imap.logout()
                except Exception:
                    pass
            self.imap = None

    @staticmethod
    def _select(m, folder, readonly):
        typ, data = m.select(_q(folder), readonly=readonly)
        if typ != "OK":
            raise MailError(f"Не удалось открыть папку «{folder_title(folder)}»")
        return int(data[0]) if data and data[0] else 0

    @staticmethod
    def _inbox_unread(m):
        typ, st = m.status("INBOX", "(UNSEEN)")
        if typ == "OK" and st and st[0]:
            mm = re.search(rb"UNSEEN (\d+)", st[0])
            if mm:
                return int(mm.group(1))
        return 0

    # --- папки ----------------------------------------------------------
    def folders(self):
        if self._folders is not None:
            return self._folders
        items = self._with(lambda m: _parse_list(m.list()[1]))
        roles, others = {"inbox": "INBOX"}, []
        for name, flags, delim in items:
            if flags & HIDDEN_FLAGS or name.upper() == "INBOX":
                continue
            role = next((ROLE_FLAGS[f] for f in flags if f in ROLE_FLAGS), None)
            if role and role not in roles:
                roles[role] = name
            else:
                others.append((name, delim))
        for role, names in ROLE_NAMES.items():
            if role in roles:
                continue
            for item in others:
                if folder_title(*item).lower() in names:
                    roles[role] = item[0]
                    others.remove(item)
                    break
        others.sort(key=lambda x: folder_title(*x).lower())
        self._folders = {
            "roles": roles,
            "others": [{"name": n, "title": folder_title(n, d)} for n, d in others],
        }
        return self._folders

    def resolve(self, folder: str):
        """'inbox'/'sent'/… → настоящее имя папки; 'name:<имя>' → <имя>."""
        if folder.startswith("name:"):
            return folder[5:]
        return self.folders()["roles"].get(folder)

    # --- список писем ---------------------------------------------------
    def list_folder(self, folder, limit=60, offset=0):
        def fn(m):
            unread = self._inbox_unread(m)
            total = self._select(m, folder, readonly=True)
            hi = total - offset
            if hi < 1:
                return [], unread, False
            lo = max(1, hi - limit + 1)
            typ, data = m.fetch(f"{lo}:{hi}",
                                "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
            if typ != "OK":
                raise MailError("Не удалось получить список писем")
            return _parse_headers(data), unread, lo > 1

        return self._with(fn)

    def inbox_unread(self):
        return self._with(self._inbox_unread)

    # --- одно письмо ----------------------------------------------------
    def fetch_raw(self, folder, uid):
        key = (folder, str(int(uid)))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        def fn(m):
            self._select(m, folder, readonly=False)  # открытие помечает прочитанным
            typ, data = m.uid("fetch", key[1], "(BODY[])")
            for item in data or []:
                if isinstance(item, tuple):
                    return item[1]
            raise MailError("Письмо не найдено (возможно, его удалили)")

        raw = self._with(fn)
        self._cache[key] = raw
        while len(self._cache) > 15:
            self._cache.popitem(last=False)
        return raw

    # --- действия -------------------------------------------------------
    def set_flag(self, folder, uid, flag, on):
        uid = str(int(uid))

        def fn(m):
            self._select(m, folder, readonly=False)
            typ, _ = m.uid("store", uid, "+FLAGS" if on else "-FLAGS", f"({flag})")
            if typ != "OK":
                raise MailError("Сервер не принял изменение")

        self._with(fn)
        if flag == "\\Seen":
            self._cache.pop((folder, uid), None)  # чтобы повторное открытие снова отметило прочитанным

    def move(self, folder, uid, dest):
        uid = str(int(uid))

        def fn(m):
            self._select(m, folder, readonly=False)
            caps = getattr(m, "capabilities", ())
            if "MOVE" in caps:
                typ, _ = m.uid("move", uid, _q(dest))
            else:
                typ, _ = m.uid("copy", uid, _q(dest))
                if typ == "OK":
                    m.uid("store", uid, "+FLAGS", "(\\Deleted)")
                    if "UIDPLUS" in caps:
                        m.uid("expunge", uid)
                    else:
                        m.expunge()
            if typ != "OK":
                raise MailError(f"Не удалось переместить письмо в «{folder_title(dest)}»")

        self._with(fn)
        self._cache.pop((folder, uid), None)

    def delete_forever(self, folder, uid):
        uid = str(int(uid))

        def fn(m):
            self._select(m, folder, readonly=False)
            m.uid("store", uid, "+FLAGS", "(\\Deleted)")
            if "UIDPLUS" in getattr(m, "capabilities", ()):
                m.uid("expunge", uid)
            else:
                m.expunge()

        self._with(fn)
        self._cache.pop((folder, uid), None)


def _parse_headers(data):
    entries = []
    cur = None
    for item in data or []:
        if isinstance(item, tuple):
            cur = [item[0], item[1]]
            entries.append(cur)
        elif isinstance(item, bytes) and cur is not None:
            cur[0] += b" " + item  # некоторые серверы шлют FLAGS после тела
    out = []
    for meta, payload in entries:
        mu = re.search(rb"UID (\d+)", meta)
        if not mu:
            continue
        mf = re.search(rb"FLAGS \(([^)]*)\)", meta)
        flags = mf.group(1).decode("ascii", "replace") if mf else ""
        msg = email.message_from_bytes(payload or b"")
        name, addr = parseaddr(str(msg.get("From", "")))
        to = [(_dh(n) or a) for n, a in getaddresses([str(msg.get("To", ""))]) if n or a]
        out.append({
            "uid": int(mu.group(1)),
            "seen": "\\Seen" in flags,
            "flagged": "\\Flagged" in flags,
            "from_name": _dh(name) or addr,
            "from_addr": addr,
            "to": ", ".join(to),
            "subject": _dh(msg.get("Subject")) or "(без темы)",
            "ts": _ts(msg.get("Date")),
        })
    return out


# --- разбор полного письма -------------------------------------------------
def _content(part) -> str:
    try:
        c = part.get_content()
        if isinstance(c, bytes):
            raise ValueError
        return c
    except Exception:
        payload = part.get_payload(decode=True) or b""
        cs = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(cs, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


def _text_to_html(text: str) -> str:
    esc = html.escape(text)
    esc = re.sub(r'(https?://[^\s<>"]+)', r'<a href="\1">\1</a>', esc)
    return f'<pre style="white-space:pre-wrap;font-family:inherit;margin:0">{esc}</pre>'


def _attachment_parts(msg):
    parts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        try:
            filename = part.get_filename()
        except Exception:
            filename = None
        disp = part.get_content_disposition()
        inline_img = disp == "inline" and part.get("Content-ID")
        if disp == "attachment" or (filename and not inline_img):
            parts.append((part, _dh(filename) if filename else "вложение"))
    return parts


def parse_message(raw: bytes) -> dict:
    head = email.message_from_bytes(raw)  # compat32 — устойчив к кривым заголовкам
    msg = email.message_from_bytes(raw, policy=policy.default)

    html_body = text_body = None
    try:
        p = msg.get_body(preferencelist=("html",))
        if p is not None:
            html_body = _content(p)
    except Exception:
        pass
    if html_body is None:
        try:
            p = msg.get_body(preferencelist=("plain",))
            if p is not None:
                text_body = _content(p)
        except Exception:
            pass
    if html_body is None and text_body is None and not msg.is_multipart():
        text_body = _content(msg)

    if html_body is not None:
        cids = {}
        for part in msg.walk():
            cid = part.get("Content-ID")
            if cid and part.get_content_maintype() == "image":
                data = part.get_payload(decode=True)
                if data and len(data) < 3_000_000:
                    cids[str(cid).strip("<> ")] = (
                        f"data:{part.get_content_type()};base64,"
                        + base64.b64encode(data).decode("ascii"))
        if cids:
            html_body = re.sub(r"cid:([^\"'\s)>]+)",
                               lambda m: cids.get(unquote(m.group(1)), m.group(0)),
                               html_body)
        body = html_body
    else:
        body = _text_to_html(text_body or "")

    attachments = []
    for i, (part, name) in enumerate(_attachment_parts(msg)):
        data = part.get_payload(decode=True) or b""
        attachments.append({"i": i, "name": name, "size": len(data),
                            "type": part.get_content_type()})

    name, addr = parseaddr(str(head.get("From", "")))
    return {
        "subject": _dh(head.get("Subject")) or "(без темы)",
        "from_name": _dh(name) or addr,
        "from_addr": addr,
        "to": _dh(head.get("To")),
        "cc": _dh(head.get("Cc")),
        "ts": _ts(head.get("Date")),
        "html": body,
        "attachments": attachments,
    }


def get_attachment(raw: bytes, index: int):
    msg = email.message_from_bytes(raw, policy=policy.default)
    parts = _attachment_parts(msg)
    if not 0 <= index < len(parts):
        raise MailError("Вложение не найдено")
    part, name = parts[index]
    return name, part.get_content_type(), part.get_payload(decode=True) or b""
