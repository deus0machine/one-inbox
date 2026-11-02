"""One Inbox — все ящики в одном окне.

Запуск: ярлык «Почта» (или `pythonw app.py`). Программа живёт в трее:
локальный сервер на 127.0.0.1 + фоновая проверка почты + уведомления.
Окно — это Edge в режиме приложения; его можно закрывать, программа
останется в трее. Полностью выйти — «Выход» в меню иконки в трее.
"""
import ctypes
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import mail_client
import secrets_store

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
UI = os.path.join(BASE, "ui")
ICON = os.path.join(BASE, "icon.ico")
INSTANCE = os.path.join(DATA, "instance.json")
os.makedirs(DATA, exist_ok=True)

logging.basicConfig(filename=os.path.join(DATA, "oneinbox.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
log = logging.getLogger("oneinbox")

TOKEN = secrets.token_urlsafe(24)
COLORS = ["#e5484d", "#3e63dd", "#30a46c", "#f76b15", "#8e4ec6", "#12a594", "#d6409f", "#ad7f58"]
INBOX_LIMIT = 60
POLL_SECONDS = 120
HEADLESS = bool(os.environ.get("ONEINBOX_NO_BROWSER"))  # для тестов: без окна и без трея

URL = ""
TRAY = None


# --- хранилище ящиков ------------------------------------------------------
class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.accounts = []   # публичные данные
        self.passwords = {}  # id -> пароль (только в памяти)
        self.boxes = {}      # id -> Mailbox
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for a in json.load(f):
                    try:
                        self.passwords[a["id"]] = secrets_store.unprotect(a.pop("password_enc"))
                    except Exception as e:
                        log.error("Не расшифровался пароль %s: %s", a.get("email"), e)
                        continue
                    self.accounts.append(a)

    def _save(self):
        rows = [dict(a, password_enc=secrets_store.protect(self.passwords[a["id"]]))
                for a in self.accounts]
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def list(self):
        with self.lock:
            return [dict(a) for a in self.accounts]

    def get(self, acc_id):
        with self.lock:
            return next((dict(a) for a in self.accounts if a["id"] == acc_id), None)

    def box(self, acc_id):
        with self.lock:
            acc = next((a for a in self.accounts if a["id"] == acc_id), None)
            if acc is None:
                raise mail_client.MailError("Ящик не найден")
            if acc_id not in self.boxes:
                self.boxes[acc_id] = mail_client.Mailbox(
                    acc["email"], self.passwords[acc_id], acc["host"], acc.get("port", 993))
            return self.boxes[acc_id]

    def add(self, address, password, name, host=None):
        address = address.strip()
        if "@" not in address:
            raise mail_client.MailError("Это не похоже на адрес почты")
        if not password:
            raise mail_client.MailError("Нужен пароль приложения")
        host = (host or "").strip() or mail_client.detect_host(address)
        if not host:
            raise mail_client.MailError("Не знаю IMAP-сервер для этого домена — укажи его вручную")
        with self.lock:
            if any(a["email"].lower() == address.lower() for a in self.accounts):
                raise mail_client.MailError("Этот ящик уже добавлен")
        box = mail_client.Mailbox(address, password, host)
        box.check()  # проверяем логин до сохранения
        with self.lock:
            acc = {"id": uuid.uuid4().hex[:8], "email": address, "name": name.strip(),
                   "host": host, "port": 993,
                   "color": COLORS[len(self.accounts) % len(COLORS)]}
            self.accounts.append(acc)
            self.passwords[acc["id"]] = box.password
            self.boxes[acc["id"]] = box
            self._save()
            return dict(acc)

    def rename(self, acc_id, name):
        with self.lock:
            acc = next((a for a in self.accounts if a["id"] == acc_id), None)
            if acc is None:
                raise mail_client.MailError("Ящик не найден")
            acc["name"] = (name or "").strip()[:60]
            self._save()
            return dict(acc)

    def remove(self, acc_id):
        with self.lock:
            self.accounts = [a for a in self.accounts if a["id"] != acc_id]
            self.passwords.pop(acc_id, None)
            box = self.boxes.pop(acc_id, None)
            self._save()
        if box:
            box.close()


store = Store(os.path.join(DATA, "accounts.json"))


class Settings:
    """Мелкие настройки: уведомления и отправители, чьи картинки показываем сразу."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"trusted_senders": [], "notifications": True}
        try:
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def trust(self, addr, on):
        addr = (addr or "").strip().lower()
        if "@" not in addr:
            raise mail_client.MailError("Непонятный адрес отправителя")
        with self.lock:
            s = set(self.data["trusted_senders"])
            s.add(addr) if on else s.discard(addr)
            self.data["trusted_senders"] = sorted(s)
            self._save()
        return self.get()

    @property
    def notifications(self):
        with self.lock:
            return bool(self.data.get("notifications", True))

    def set_notifications(self, on):
        with self.lock:
            self.data["notifications"] = bool(on)
            self._save()
        log.info("Уведомления %s", "включены" if on else "выключены")
        return self.get()


settings = Settings(os.path.join(DATA, "settings.json"))


# --- новые письма, счётчики, уведомления -----------------------------------
def _plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _window_in_front():
    if HEADLESS:
        return False
    try:
        import taskbar
        return taskbar.is_foreground()
    except Exception:
        return False


class MailState:
    """Следит за «Входящими»: что пришло нового, сколько непрочитанного.

    «Новые» — непрочитанные письма, пришедшие, пока окно не было перед глазами.
    Сбрасываются, когда пользователь открывает окно.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.unread = {}    # id ящика -> непрочитанных во «Входящих»
        self.baseline = {}  # id ящика -> самый большой UID, который мы уже видели
        self.new = {}       # (id ящика, uid) -> письмо
        self.pending = []   # (ящик, письмо) — ещё не показанные уведомления
        self.version = 0    # растёт при новой почте; окно по нему понимает, что пора обновиться

    def on_inbox(self, acc, msgs, unread):
        """Свежая первая страница «Входящих» ящика acc (из фоновой проверки или из окна)."""
        aid = acc["id"]
        with self.lock:
            self.unread[aid] = unread
            uids = [m["uid"] for m in msgs]
            top = max(uids, default=0)
            base = self.baseline.get(aid)
            if base is None:  # первый взгляд на ящик: всё, что есть сейчас, — не «новое»
                self.baseline[aid] = top
                return
            seen = {m["uid"] for m in msgs if m["seen"]}
            present, lowest = set(uids), min(uids, default=0)
            for key in list(self.new):  # прочитали/удалили где-то ещё — уже не новое
                if key[0] == aid and (key[1] in seen or (key[1] not in present and key[1] >= lowest)):
                    del self.new[key]
            fresh = [m for m in msgs if m["uid"] > base and not m["seen"]]
            self.baseline[aid] = max(base, top)
            if fresh:
                self.version += 1
                if not _window_in_front():
                    for m in fresh:
                        self.new[(aid, m["uid"])] = m
                        self.pending.append((acc, m))

    def set_unread(self, aid, n):
        with self.lock:
            self.unread[aid] = n

    def forget(self, aid, uid):
        with self.lock:
            self.new.pop((aid, int(uid)), None)

    def drop_account(self, aid):
        with self.lock:
            self.unread.pop(aid, None)
            self.baseline.pop(aid, None)
            for key in [k for k in self.new if k[0] == aid]:
                del self.new[key]

    def ack(self):
        with self.lock:
            self.new.clear()
            self.pending.clear()

    def flush(self):
        """Показать накопившиеся уведомления и обновить счётчики в трее/на панели задач."""
        with self.lock:
            pending, self.pending = self.pending, []
            new, unread = len(self.new), sum(self.unread.values())
        if pending and settings.notifications and TRAY:
            TRAY.notify(*self._notification_text(pending))
        if TRAY:
            TRAY.update(new, unread)

    @staticmethod
    def _notification_text(pending):
        def label(acc):
            return acc.get("name") or acc["email"]

        if len(pending) == 1:
            acc, m = pending[0]
            return (m["from_name"] or m["from_addr"] or "Новое письмо",
                    f'{m["subject"]}\n{label(acc)}')
        n = len(pending)
        lines = [f'{m["from_name"] or m["from_addr"]}: {m["subject"]}' for _, m in pending[:3]]
        if n > 3:
            lines.append(f"…и ещё {n - 3}")
        return f'{n} {_plural(n, "новое письмо", "новых письма", "новых писем")}', "\n".join(lines)


mail_state = MailState()
poll_now = threading.Event()


def poll_once():
    accs = store.list()
    if not accs:
        return mail_state.flush()

    def work(a):
        msgs, n, _ = store.box(a["id"]).list_folder("INBOX", 30, 0)
        return msgs, n

    with ThreadPoolExecutor(max_workers=len(accs)) as ex:
        futs = {ex.submit(work, a): a for a in accs}
        for fut, a in futs.items():
            try:
                msgs, n = fut.result()
                mail_state.on_inbox(a, msgs, n)
            except Exception as e:
                log.warning("Фоновая проверка %s: %s", a["email"], e)
    mail_state.flush()


def poller():
    time.sleep(2)
    while True:
        try:
            poll_once()
        except Exception:
            log.exception("Ошибка фоновой проверки")
        poll_now.wait(POLL_SECONDS)
        poll_now.clear()


# --- запросы окна ----------------------------------------------------------
def load_list(folder, acc_filter, offsets, limits):
    """Письма папки folder ('inbox', 'sent', … или 'name:<имя>') из одного или всех ящиков.

    offsets/limits — {id ящика: число}: сколько писем пропустить с конца и сколько взять.
    """
    accs = [a for a in store.list() if acc_filter in ("all", a["id"])]
    result = {"messages": [], "errors": [], "unread": {}, "more": {}, "next": {}}
    if not accs:
        return result

    def work(a):
        box = store.box(a["id"])
        real = box.resolve(folder)
        if real is None:  # в этом ящике нет такой папки
            return [], box.inbox_unread(), False, None, 0, 0
        off = max(0, int(offsets.get(a["id"], 0)))
        lim = max(1, min(300, int(limits.get(a["id"], INBOX_LIMIT))))
        msgs, n, more = box.list_folder(real, lim, off)
        return msgs, n, more, real, off, off + lim

    with ThreadPoolExecutor(max_workers=len(accs)) as ex:
        futs = {ex.submit(work, a): a for a in accs}
        for fut, a in futs.items():
            try:
                msgs, n, more, real, off, nxt = fut.result()
                if folder == "inbox" and off == 0:
                    mail_state.on_inbox(a, msgs, n)
                else:
                    mail_state.set_unread(a["id"], n)
                for m in msgs:
                    m["acc"] = a["id"]
                    m["folder"] = real
                    m["key"] = f'{a["id"]}|{real}|{m["uid"]}'
                result["messages"].extend(msgs)
                result["unread"][a["id"]] = n
                result["more"][a["id"]] = more
                result["next"][a["id"]] = nxt
            except Exception as e:
                log.warning("Ошибка ящика %s: %s", a["email"], e)
                result["errors"].append({"acc": a["id"], "email": a["email"], "error": str(e)})
    mail_state.flush()
    result["messages"].sort(key=lambda m: m["ts"], reverse=True)
    return result


def do_action(b):
    box = store.box(b["acc"])
    folder, uid, act = b["folder"], b["uid"], b["action"]
    if act in ("seen", "unseen"):
        box.set_flag(folder, uid, "\\Seen", act == "seen")
    elif act in ("flag", "unflag"):
        box.set_flag(folder, uid, "\\Flagged", act == "flag")
    elif act in ("trash", "spam"):
        role = "trash" if act == "trash" else "junk"
        dest = box.resolve(role)
        if not dest:
            raise mail_client.MailError(
                "В этом ящике не нашлось папки " + ("«Корзина»" if role == "trash" else "«Спам»"))
        box.move(folder, uid, dest)
    elif act in ("notspam", "restore"):
        box.move(folder, uid, "INBOX")
    elif act == "delete":
        box.delete_forever(folder, uid)
    else:
        raise mail_client.MailError("Неизвестное действие")
    if folder == "INBOX" and act in ("seen", "trash", "spam", "delete"):
        mail_state.forget(b["acc"], uid)
    n = box.inbox_unread()
    mail_state.set_unread(b["acc"], n)
    mail_state.flush()
    return {"ok": True, "unread": n}


# --- HTTP ------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body: bytes, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _authed(self, q):
        return (self.headers.get("X-Token") == TOKEN
                or q.get("t", [""])[0] == TOKEN)

    def _route(self, method):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        path = url.path

        if method == "GET" and not path.startswith("/api/"):
            return self._static(path)
        if not self._authed(q):
            return self._json({"error": "forbidden"}, 403)

        if path == "/api/ping":
            return self._json({"v": mail_state.version, "notifications": settings.notifications})
        if path == "/api/bye":  # окно закрыли — программа остаётся в трее
            return self._json({"ok": True})
        if path == "/api/quit" and method == "POST":
            threading.Timer(0.3, quit_app).start()
            return self._json({"ok": True})
        if path == "/api/show":  # повторный запуск ярлыка — показать окно
            open_window()
            return self._json({"ok": True})
        if path == "/api/ack" and method == "POST":  # окно перед глазами — «новые» больше не новые
            mail_state.ack()
            mail_state.flush()
            return self._json({"ok": True})
        if path == "/api/settings" and method == "GET":
            return self._json(settings.get())
        if path == "/api/settings" and method == "POST":
            b = self._body()
            if "notifications" in b:
                settings.set_notifications(b["notifications"])
            return self._json(settings.get())
        if path == "/api/trust" and method == "POST":
            b = self._body()
            return self._json(settings.trust(b.get("addr"), bool(b.get("on", True))))
        if path == "/api/accounts" and method == "GET":
            return self._json(store.list())
        if path == "/api/accounts" and method == "POST":
            b = self._body()
            acc = store.add(b.get("email", ""), b.get("password", ""),
                            b.get("name", ""), b.get("host"))
            return self._json(acc)
        if path.startswith("/api/accounts/") and method == "POST":
            acc = store.rename(path.rsplit("/", 1)[-1], self._body().get("name", ""))
            return self._json(acc)
        if path.startswith("/api/accounts/") and method == "DELETE":
            aid = path.rsplit("/", 1)[-1]
            store.remove(aid)
            mail_state.drop_account(aid)
            mail_state.flush()
            return self._json({"ok": True})
        if path == "/api/list":
            return self._json(load_list(
                q.get("folder", ["inbox"])[0], q.get("acc", ["all"])[0],
                json.loads(q.get("offsets", ["{}"])[0]), json.loads(q.get("limits", ["{}"])[0])))
        if path == "/api/folders":
            f = store.box(q["acc"][0]).folders()
            return self._json({"roles": sorted(f["roles"]), "others": f["others"]})
        if path == "/api/action" and method == "POST":
            return self._json(do_action(self._body()))
        if path == "/api/message":
            aid, folder, uid = q["acc"][0], q["folder"][0], q["uid"][0]
            data = mail_client.parse_message(store.box(aid).fetch_raw(folder, uid))
            if folder == "INBOX":
                mail_state.forget(aid, uid)
                mail_state.flush()
            return self._json(data)
        if path == "/api/attachment":
            box = store.box(q["acc"][0])
            name, ctype, data = mail_client.get_attachment(
                box.fetch_raw(q["folder"][0], q["uid"][0]), int(q["i"][0]))
            return self._send(200, data, ctype or "application/octet-stream", {
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
        return self._json({"error": "not found"}, 404)

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        full = os.path.normpath(os.path.join(UI, path.lstrip("/")))
        if not full.startswith(UI + os.sep) or not os.path.isfile(full):
            return self._json({"error": "not found"}, 404)
        with open(full, "rb") as f:
            body = f.read()
        ext = os.path.splitext(full)[1]
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
        if ext == ".html":
            body = body.replace(b"{{TOKEN}}", TOKEN.encode())
        self._send(200, body, ctype)

    def _safe(self, method):
        try:
            self._route(method)
        except mail_client.MailError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            log.exception("Ошибка обработки %s %s", method, self.path)
            self._json({"error": f"Внутренняя ошибка: {e}"}, 500)

    def do_GET(self):
        self._safe("GET")

    def do_POST(self):
        self._safe("POST")

    def do_DELETE(self):
        self._safe("DELETE")


# --- окно ------------------------------------------------------------------
def find_browser():
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return shutil.which("msedge") or shutil.which("chrome")


def harden_profile(profile):
    """Выключает в отдельном профиле Edge всё лишнее: пароли, автозаполнение, переводчик, фон."""
    path = os.path.join(profile, "Default", "Preferences")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as f:
                prefs = json.load(f)
        except (OSError, ValueError):
            prefs = {}

        def put(keys, val):
            d = prefs
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = val

        put(["credentials_enable_service"], False)
        put(["credentials_enable_autosignin"], False)
        put(["profile", "password_manager_enabled"], False)
        put(["autofill", "enabled"], False)
        put(["autofill", "profile_enabled"], False)
        put(["autofill", "credit_card_enabled"], False)
        put(["translate", "enabled"], False)
        put(["browser", "has_seen_welcome_page"], True)
        put(["background_mode", "enabled"], False)  # закрыл окно — Edge выгружается целиком
        with open(path, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
    except Exception:
        log.exception("Не удалось настроить профиль Edge")


def _relaunch_cmd():
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pythonw}" "{os.path.join(BASE, "app.py")}"'


def _brand():
    try:
        import taskbar
        if taskbar.brand_window(_relaunch_cmd(), ICON):
            if TRAY:
                TRAY.refresh()  # окно новое — заново повесить значок с числом
        else:
            log.warning("Окно программы не найдено — иконку на панели задач не поменять")
    except Exception:
        log.exception("Не удалось поменять иконку на панели задач")


_open_lock = threading.Lock()
_last_launch = 0.0


def open_window():
    """Показать окно: вытащить на передний план, если открыто, иначе запустить."""
    global _last_launch
    if HEADLESS:
        return
    import taskbar
    hwnd = taskbar.find_app_window()
    if hwnd:
        taskbar.bring_to_front(hwnd)
        return
    with _open_lock:
        if time.time() - _last_launch < 5:  # окно уже открывается
            return
        _last_launch = time.time()
    exe = find_browser()
    if not exe:
        webbrowser.open(URL)
        return
    profile = os.path.join(DATA, "browser-profile")
    harden_profile(profile)
    subprocess.Popen([exe, f"--app={URL}", f"--user-data-dir={profile}",
                      "--no-first-run", "--no-default-browser-check",
                      "--disable-sync", "--disable-extensions",
                      "--window-size=1320,860"])
    threading.Thread(target=_brand, daemon=True).start()


# --- запуск и выход --------------------------------------------------------
def signal_existing():
    """Если программа уже запущена — попросить её показать окно и выйти."""
    try:
        with open(INSTANCE, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return False
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # разрешить ей вывести окно вперёд
        urllib.request.urlopen(f'http://127.0.0.1:{info["port"]}/api/show?t={info["token"]}', timeout=3)
        return True
    except Exception:
        return False


def ensure_start_menu_shortcut():
    """Ярлык в «Пуске» с нашим AppUserModelID — Windows подписывает им уведомления."""
    try:
        import shortcut
        import taskbar
        lnk = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs", "Почта.lnk")
        if os.path.exists(lnk):
            return
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        shortcut.create_shortcut(lnk, pythonw, f'"{os.path.join(BASE, "app.py")}"', BASE, ICON,
                                 "Все почтовые ящики в одном окне", taskbar.APP_ID)
        log.info("Создан ярлык в меню «Пуск»: %s", lnk)
    except Exception:
        log.exception("Не удалось создать ярлык в меню «Пуск»")


def quit_app():
    log.info("Выход из программы")
    if TRAY:
        TRAY.remove()
    try:
        import taskbar
        hwnd = taskbar.find_app_window()
        if hwnd:
            taskbar.close_window(hwnd)
    except Exception:
        pass
    for a in store.list():
        try:
            store.box(a["id"]).close()
        except Exception:
            pass
    try:
        with open(INSTANCE, encoding="utf-8") as f:
            mine = json.load(f).get("token") == TOKEN
        if mine:  # удаляем после закрытия: открытый файл Windows удалить не даст
            os.remove(INSTANCE)
    except (OSError, ValueError):
        pass
    os._exit(0)


def main():
    global URL, TRAY
    if not HEADLESS and signal_existing():
        return

    if not HEADLESS:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # чёткие иконки на 125–200%
        except Exception:
            pass
        import taskbar
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(taskbar.APP_ID)
        if not os.path.exists(ICON):  # иконка не хранится в репозитории — рисуем при первом запуске
            try:
                import icons
                icons.write_ico(ICON)
            except Exception:
                log.exception("Не удалось создать иконку")
        ensure_start_menu_shortcut()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    URL = f"http://127.0.0.1:{port}/"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Старт на %s", URL)

    if HEADLESS:
        print(URL, TOKEN, flush=True)
        threading.Thread(target=poller, daemon=True).start()
        while True:
            time.sleep(3600)

    with open(INSTANCE, "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": TOKEN, "pid": os.getpid()}, f)

    import tray
    TRAY = tray.Tray(on_open=lambda: threading.Thread(target=open_window, daemon=True).start(),
                     on_exit=quit_app,
                     is_notify_on=lambda: settings.notifications,
                     toggle_notify=lambda: settings.set_notifications(not settings.notifications))
    threading.Thread(target=poller, daemon=True).start()
    if os.environ.get("ONEINBOX_TEST_NOTIFY"):
        TRAY.notify("Проверка уведомлений", "Так будут выглядеть уведомления о новых письмах")
    if "--tray" not in sys.argv:  # с --tray стартуем сразу свёрнутыми в трей
        threading.Thread(target=open_window, daemon=True).start()
    TRAY.run()  # цикл сообщений трея — до «Выход»


if __name__ == "__main__":
    if sys.stdout is None:  # pythonw
        sys.stdout = sys.stderr = open(os.devnull, "w")
    main()
