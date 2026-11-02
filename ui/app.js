const T = window.TOKEN;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const enc = encodeURIComponent;

const FOLDERS = [
  ["inbox", "Входящие"], ["sent", "Отправленные"], ["drafts", "Черновики"],
  ["junk", "Спам"], ["trash", "Корзина"],
];

const state = {
  accounts: [], messages: [], unread: {}, errors: [],
  filter: "all",        // "all" или id ящика
  folder: "inbox",      // роль папки или "name:<имя>"
  more: {}, next: {},   // есть ли ещё письма / с какого места грузить, по ящикам
  accFolders: {},       // id ящика -> дополнительные папки
  selected: null, current: null,
  search: "", loading: false, loadingMore: false, req: 0,
};

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "X-Token": T, "Content-Type": "application/json" } });
  let data = null;
  try { data = await r.json(); } catch {}
  if (!r.ok) throw new Error((data && data.error) || "Ошибка " + r.status);
  return data;
}

const accById = (id) => state.accounts.find((a) => a.id === id);
const accLabel = (a) => (a ? a.name || a.email : "?");
const isOutgoing = () => state.folder === "sent" || state.folder === "drafts";

function fmtDate(ts, full = false) {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = new Date();
  if (full) return d.toLocaleString("ru-RU", { day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "вчера";
  const opt = d.getFullYear() === now.getFullYear() ? { day: "numeric", month: "short" } : { day: "numeric", month: "short", year: "numeric" };
  return d.toLocaleDateString("ru-RU", opt);
}

function fmtSize(n) {
  if (n < 1024) return n + " Б";
  if (n < 1024 * 1024) return Math.round(n / 1024) + " КБ";
  return (n / 1024 / 1024).toFixed(1) + " МБ";
}

let toastTimer;
function toast(text, isError = false) {
  const t = $("#toast");
  t.textContent = text;
  t.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), isError ? 5000 : 2500);
}

// ---------- sidebar ----------
function renderSidebar() {
  const total = Object.values(state.unread).reduce((s, n) => s + n, 0);
  document.title = total ? `(${total}) Почта` : "Почта";
  let h = `<div class="acc ${state.filter === "all" ? "active" : ""}" data-id="all">
      <span class="dot" style="background:var(--accent)"></span>
      <span class="label">Все ящики</span>
      ${total ? `<span class="count">${total}</span>` : ""}
    </div>`;
  if (state.accounts.length) h += `<div class="sep"></div>`;
  for (const a of state.accounts) {
    const n = state.unread[a.id] || 0;
    h += `<div class="acc ${state.filter === a.id ? "active" : ""}" data-id="${a.id}" title="${esc(a.email)}">
      <span class="dot" style="background:${a.color}"></span>
      <span class="label">${esc(accLabel(a))}${a.name ? `<small>${esc(a.email)}</small>` : ""}</span>
      ${n ? `<span class="count has-del">${n}</span>` : ""}
      <button class="ren" data-ren="${a.id}" title="Переименовать">✎</button>
      <button class="del" data-del="${a.id}" title="Удалить ящик">×</button>
    </div>`;
  }
  $("#accounts").innerHTML = h;
}

$("#accounts").addEventListener("click", async (e) => {
  const ren = e.target.closest("[data-ren]");
  if (ren) {
    e.stopPropagation();
    const a = accById(ren.dataset.ren);
    const name = prompt(`Название для ${a.email}\n(оставь пустым, чтобы показывать адрес)`, a.name || "");
    if (name === null) return;
    try {
      await api("/api/accounts/" + a.id, { method: "POST", body: JSON.stringify({ name }) });
      await loadAccounts();
      renderList();
    } catch (err) { toast(err.message, true); }
    return;
  }
  const del = e.target.closest("[data-del]");
  if (del) {
    e.stopPropagation();
    const a = accById(del.dataset.del);
    if (!confirm(`Убрать ящик ${a.email} из программы?\n(В самой почте ничего не удалится.)`)) return;
    await api("/api/accounts/" + a.id, { method: "DELETE" });
    delete state.accFolders[a.id];
    delete state.unread[a.id];
    if (state.filter === a.id) { state.filter = "all"; state.folder = "inbox"; }
    await loadAccounts();
    renderFolders();
    loadList();
    return;
  }
  const item = e.target.closest(".acc");
  if (!item) return;
  selectAccount(item.dataset.id);
});

function selectAccount(id) {
  if (state.filter === id) return;
  const wasNamed = state.folder.startsWith("name:");
  state.filter = id;
  if (wasNamed) state.folder = "inbox";
  renderSidebar();
  renderFolders();
  if (id !== "all") loadFolders(id);
  if (wasNamed) loadList(); else renderList(); // обычные папки грузятся сразу по всем ящикам
}

// ---------- folders ----------
async function loadFolders(id) {
  if (state.accFolders[id]) return;
  try {
    const f = await api(`/api/folders?acc=${enc(id)}`);
    state.accFolders[id] = f.others;
    if (state.filter === id) renderFolders();
  } catch {}
}

function renderFolders() {
  let h = FOLDERS.map(([id, t]) =>
    `<button class="ftab ${state.folder === id ? "active" : ""}" data-folder="${id}">${t}</button>`).join("");
  const others = state.filter !== "all" && state.accFolders[state.filter];
  if (others && others.length) {
    const named = state.folder.startsWith("name:");
    h += `<select class="fmore ${named ? "active" : ""}" id="fmore">
      <option value="">Ещё…</option>
      ${others.map((o) => `<option value="name:${esc(o.name)}" ${state.folder === "name:" + o.name ? "selected" : ""}>${esc(o.title)}</option>`).join("")}
    </select>`;
  }
  $("#folders").innerHTML = h;
}

$("#folders").addEventListener("click", (e) => {
  const b = e.target.closest("[data-folder]");
  if (b) setFolder(b.dataset.folder);
});
$("#folders").addEventListener("change", (e) => {
  if (e.target.id === "fmore" && e.target.value) setFolder(e.target.value);
});

function setFolder(f) {
  if (state.folder === f) return;
  state.folder = f;
  renderFolders();
  loadList();
}

// ---------- list ----------
function visibleMessages() {
  const q = state.search.trim().toLowerCase();
  return state.messages.filter((m) =>
    (state.filter === "all" || m.acc === state.filter) &&
    (!q || (m.subject + " " + m.from_name + " " + m.from_addr + " " + m.to).toLowerCase().includes(q)));
}

function hasMore() {
  return Object.entries(state.more).some(([acc, v]) => v && (state.filter === "all" || acc === state.filter));
}

function renderList() {
  const errs = state.errors.filter((e) => state.filter === "all" || e.acc === state.filter || e.acc === "all");
  $("#errors").innerHTML = errs.map((e) => `<div class="err">⚠ ${esc(e.email)}: ${esc(e.error)}</div>`).join("");

  if (!state.accounts.length) {
    $("#list").innerHTML = `<div class="notice">Пока нет ни одного ящика.<br><button onclick="openAdd()">Добавить первый ящик</button></div>`;
    return;
  }
  const msgs = visibleMessages();
  if (!msgs.length) {
    $("#list").innerHTML = `<div class="notice">${state.loading ? "Загружаю письма…" : "Писем нет"}</div>`;
    return;
  }
  const out = isOutgoing();
  let h = msgs.map((m) => {
    const a = accById(m.acc);
    const who = out ? "Кому: " + (m.to || "—") : (m.from_name || m.from_addr);
    return `<div class="msg ${m.seen ? "" : "unread"} ${state.selected === m.key ? "selected" : ""}" data-key="${esc(m.key)}" title="${esc(out ? m.to : m.from_addr)} · ${esc(a ? a.email : "")}">
      <span class="bar" style="background:${a ? a.color : "transparent"}"></span>
      <span class="from">${esc(who)}</span>
      <span class="date">${m.flagged ? '<span class="star">★</span>' : ""}${fmtDate(m.ts)}</span>
      <span class="subj">${esc(m.subject)}</span>
    </div>`;
  }).join("");
  if (hasMore()) {
    h += `<div class="more-wrap"><button class="more-btn" id="moreBtn" ${state.loadingMore ? "disabled" : ""}>
      ${state.loadingMore ? "Загружаю…" : "Загрузить ещё"}</button></div>`;
  }
  $("#list").innerHTML = h;
}

$("#list").addEventListener("click", (e) => {
  if (e.target.closest("#moreBtn")) return loadMore();
  const el = e.target.closest(".msg");
  if (el) openMessage(el.dataset.key);
});

$("#search").addEventListener("input", (e) => { state.search = e.target.value; renderList(); });

// ---------- loading ----------
async function loadAccounts() {
  state.accounts = await api("/api/accounts");
  renderSidebar();
}

function listAcc() {
  return state.folder.startsWith("name:") ? state.filter : "all";
}

// keep=true — тихое обновление: сохраняем столько писем, сколько уже подгружено
async function loadList({ keep = false } = {}) {
  const id = ++state.req;
  const limits = {};
  if (keep) {
    for (const m of state.messages) limits[m.acc] = (limits[m.acc] || 0) + 1;
    for (const k in limits) limits[k] = Math.max(60, limits[k]);
  } else {
    state.messages = [];
    state.more = {};
    state.errors = [];
  }
  state.loading = true;
  state.loadingMore = false;
  $("#refreshBtn").classList.add("spin");
  renderList();
  try {
    const r = await api(`/api/list?folder=${enc(state.folder)}&acc=${enc(listAcc())}&limits=${enc(JSON.stringify(limits))}`);
    if (id !== state.req) return;
    state.messages = r.messages;
    state.errors = r.errors;
    state.more = r.more;
    state.next = r.next;
    Object.assign(state.unread, r.unread);
  } catch (err) {
    if (id === state.req) state.errors = [{ acc: "all", email: "Ошибка", error: err.message }];
  } finally {
    if (id === state.req) {
      state.loading = false;
      $("#refreshBtn").classList.remove("spin");
      renderSidebar();
      renderList();
    }
  }
}

async function loadMore() {
  if (state.loadingMore) return;
  const id = state.req;
  const accs = Object.keys(state.more).filter((a) => state.more[a] && (state.filter === "all" || a === state.filter));
  if (!accs.length) return;
  state.loadingMore = true;
  renderList();
  const results = await Promise.allSettled(accs.map((acc) =>
    api(`/api/list?folder=${enc(state.folder)}&acc=${enc(acc)}&offsets=${enc(JSON.stringify({ [acc]: state.next[acc] }))}`)));
  if (id !== state.req) return; // пока грузили, переключили папку
  const seen = new Set(state.messages.map((m) => m.key));
  results.forEach((res, i) => {
    const acc = accs[i];
    if (res.status === "fulfilled") {
      for (const m of res.value.messages) if (!seen.has(m.key)) state.messages.push(m);
      state.more[acc] = res.value.more[acc];
      state.next[acc] = res.value.next[acc];
      if (res.value.errors.length) toast(res.value.errors[0].error, true);
    } else {
      toast(res.reason.message, true);
    }
  });
  state.messages.sort((a, b) => b.ts - a.ts);
  state.loadingMore = false;
  renderList();
}

$("#refreshBtn").addEventListener("click", () => loadList({ keep: true }));

// ---------- reader ----------
async function openMessage(key) {
  const m = state.messages.find((x) => x.key === key);
  if (!m) return;
  state.selected = key;
  state.current = null;
  renderList();
  const reader = $("#reader");
  reader.innerHTML = `<div class="empty">Открываю…</div>`;
  try {
    const d = await api(`/api/message?acc=${enc(m.acc)}&folder=${enc(m.folder)}&uid=${m.uid}`);
    if (state.selected !== key) return;
    if (!m.seen) {
      m.seen = true;
      if (state.folder === "inbox") state.unread[m.acc] = Math.max(0, (state.unread[m.acc] || 1) - 1);
      renderSidebar();
      renderList();
    }
    state.current = { m, d };
    renderReader();
  } catch (err) {
    if (state.selected === key) reader.innerHTML = `<div class="empty">Не удалось открыть: ${esc(err.message)}</div>`;
  }
}

function actionButtons(m) {
  const f = state.folder;
  const b = [];
  b.push(m.seen ? ["unseen", "✉", "Непрочитанное"] : ["seen", "✉", "Прочитанное"]);
  b.push(m.flagged ? ["unflag", "★", "Снять отметку"] : ["flag", "☆", "Отметить"]);
  if (f === "junk") b.push(["notspam", "✓", "Не спам"]);
  else if (f !== "trash" && f !== "sent" && f !== "drafts") b.push(["spam", "⚠", "Спам"]);
  if (f === "trash") {
    b.push(["restore", "↩", "Восстановить"]);
    b.push(["delete", "🗑", "Удалить навсегда", "danger"]);
  } else {
    b.push(["trash", "🗑", "Удалить", "danger"]);
  }
  return b.map(([act, icon, label, cls]) =>
    `<button class="act ${cls || ""}" data-act="${act}" title="${label}"><span>${icon}</span>${label}</button>`).join("");
}

// ---------- картинки как в Gmail ----------
// Внешние картинки (и шрифты/стили с чужих серверов) по умолчанию не грузятся:
// по ним отправитель узнаёт, что письмо открыто, и видит твой IP.
const REMOTE_RE = /(?:\bsrc|\bbackground|\bposter|\bhref)\s*=\s*["']?\s*(?:https?:)?\/\/|url\(\s*["']?\s*(?:https?:)?\/\//i;
const CSP_BLOCKED = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:";
const CSP_ALLOWED = "default-src 'none'; img-src * data:; style-src 'unsafe-inline' *; font-src * data:; media-src *";
const shownImages = new Set(); // письма, где картинки показали разово
let trusted = new Set();

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    trusted = new Set(s.trusted_senders || []);
  } catch {}
}

function hasRemoteContent(html) {
  // href у ссылок не считается — интересуют только загружаемые ресурсы
  return REMOTE_RE.test(html.replace(/<a\b[^>]*>/gi, ""));
}

function imagesBanner(m, d, remote, show) {
  if (!remote) return "";
  const addr = (d.from_addr || "").toLowerCase();
  if (trusted.has(addr)) {
    return `<div class="img-banner muted">Картинки от ${esc(d.from_addr)} показываются всегда ·
      <button class="link" data-img="untrust">Больше не показывать автоматически</button></div>`;
  }
  if (show) return "";
  return `<div class="img-banner">🖼 Изображения в письме скрыты.
    <button class="link" data-img="once">Показать изображения</button>
    ${addr ? `· <button class="link" data-img="trust">Всегда показывать от ${esc(d.from_addr)}</button>` : ""}</div>`;
}

async function onImagesClick(act) {
  const { m, d } = state.current;
  const addr = (d.from_addr || "").toLowerCase();
  if (act === "once") {
    shownImages.add(m.key);
  } else {
    try {
      const s = await api("/api/trust", { method: "POST", body: JSON.stringify({ addr, on: act === "trust" }) });
      trusted = new Set(s.trusted_senders);
      if (act === "untrust") shownImages.delete(m.key);
      toast(act === "trust" ? "Картинки от этого отправителя будут показываться сразу" : "Картинки снова будут скрываться");
    } catch (err) { return toast(err.message, true); }
  }
  if (state.current && state.current.m === m) renderReader();
}

function renderReader() {
  const { m, d } = state.current;
  const a = accById(m.acc);
  const remote = hasRemoteContent(d.html);
  const show = shownImages.has(m.key) || trusted.has((d.from_addr || "").toLowerCase());
  const atts = d.attachments.map((x) =>
    `<a class="att" href="/api/attachment?acc=${enc(m.acc)}&folder=${enc(m.folder)}&uid=${m.uid}&i=${x.i}&t=${enc(T)}" download>📎 ${esc(x.name)} · ${fmtSize(x.size)}</a>`).join("");
  const reader = $("#reader");
  reader.innerHTML = `
    <div class="actions-bar">${actionButtons(m)}</div>
    <div class="rhead">
      <h1>${m.flagged ? '<span class="star">★</span> ' : ""}${esc(d.subject)}</h1>
      <div class="meta"><b>${esc(d.from_name)}</b> &lt;${esc(d.from_addr)}&gt;
        <span class="badge" style="background:${a ? a.color : "#888"}">${esc(accLabel(a))}</span></div>
      <div class="meta">Кому: ${esc(d.to)}${d.cc ? " · Копия: " + esc(d.cc) : ""} · ${fmtDate(d.ts, true)}</div>
      ${atts ? `<div class="atts">${atts}</div>` : ""}
    </div>
    ${imagesBanner(m, d, remote, show)}
    <iframe sandbox="allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe>`;
  reader.querySelector("iframe").srcdoc =
    `<!doctype html><meta http-equiv="Content-Security-Policy" content="${show ? CSP_ALLOWED : CSP_BLOCKED}">
     <meta charset="utf-8"><base target="_blank">
     <style>html{scrollbar-width:thin;scrollbar-color:#c3c8d0 transparent}
     body{margin:16px 20px;font:14px/1.5 "Segoe UI",system-ui,sans-serif;color:#1c2024;background:#fff;word-wrap:break-word}
     img{max-width:100%;height:auto}table{max-width:100%}</style>` + d.html;
}

$("#reader").addEventListener("click", (e) => {
  const img = e.target.closest("[data-img]");
  if (img && state.current) return onImagesClick(img.dataset.img);
  const b = e.target.closest("[data-act]");
  if (b && state.current) doAction(state.current.m, b.dataset.act);
});

const REMOVING = { trash: "Перемещено в корзину", spam: "Перемещено в спам", notspam: "Перемещено во входящие",
  restore: "Восстановлено во входящие", delete: "Удалено навсегда" };

async function doAction(m, act) {
  if (act === "delete" && !confirm("Удалить письмо навсегда? Это нельзя отменить.")) return;
  try {
    const r = await api("/api/action", {
      method: "POST",
      body: JSON.stringify({ acc: m.acc, folder: m.folder, uid: m.uid, action: act }),
    });
    state.unread[m.acc] = r.unread;
    if (REMOVING[act]) {
      const vis = visibleMessages();
      const idx = vis.findIndex((x) => x.key === m.key);
      const neighbour = vis[idx + 1] || vis[idx - 1];
      state.messages = state.messages.filter((x) => x.key !== m.key);
      if (state.next[m.acc]) state.next[m.acc] -= 1; // в папке стало на одно письмо меньше
      toast(REMOVING[act]);
      if (neighbour) openMessage(neighbour.key);
      else { state.selected = state.current = null; $("#reader").innerHTML = `<div class="empty">Выбери письмо слева</div>`; }
    } else {
      if (act === "seen" || act === "unseen") m.seen = act === "seen";
      if (act === "flag" || act === "unflag") m.flagged = act === "flag";
      if (state.current && state.current.m === m) renderReader();
    }
    renderSidebar();
    renderList();
  } catch (err) {
    toast(err.message, true);
  }
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Delete" || !state.current) return;
  if (e.target.closest("input, select, textarea, dialog")) return;
  doAction(state.current.m, state.folder === "trash" ? "delete" : "trash");
});

// ---------- add account ----------
const dlg = $("#addDlg"), form = $("#addForm");
const HINTS = {
  yandex: `<b>Яндекс:</b> 1) в Почте: Настройки → «Почтовые программы» → включи доступ по IMAP;
    2) id.yandex.ru → Безопасность → «Пароли приложений» → Почта → создай пароль и вставь сюда.`,
  gmail: `<b>Gmail:</b> нужна включённая двухэтапная проверка. Затем открой
    myaccount.google.com/apppasswords, создай пароль приложения и вставь сюда (16 букв).`,
  mailru: `<b>Mail.ru:</b> Настройки → Безопасность → «Пароли для внешних приложений» → создай пароль.`,
};
function providerOf(email) {
  const d = (email.split("@")[1] || "").toLowerCase();
  if (/^(yandex\.|ya\.ru|narod\.ru)/.test(d)) return "yandex";
  if (d === "gmail.com" || d === "googlemail.com") return "gmail";
  if (/^(mail|bk|inbox|list|internet)\.ru$/.test(d)) return "mailru";
  return d.includes(".") ? "other" : null;
}
function updateHint() {
  const p = providerOf(form.mbxaddr.value);
  $("#hint").innerHTML = HINTS[p] || (p === "other" ? "Неизвестный домен — укажи IMAP-сервер вручную." : "");
  $("#hostRow").hidden = p !== "other";
}
form.mbxaddr.addEventListener("input", updateHint);

window.openAdd = function () {
  form.reset();
  $("#formError").textContent = "";
  updateHint();
  dlg.showModal();
};
$("#addBtn").addEventListener("click", openAdd);
$("#cancelAdd").addEventListener("click", () => dlg.close());

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#submitAdd");
  btn.disabled = true;
  btn.textContent = "Проверяю…";
  $("#formError").textContent = "";
  try {
    await api("/api/accounts", {
      method: "POST",
      body: JSON.stringify({
        email: form.mbxaddr.value, password: form.mbxkey.value,
        name: form.name.value, host: form.host.value,
      }),
    });
    dlg.close();
    await loadAccounts();
    loadList({ keep: true });
  } catch (err) {
    $("#formError").textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Проверить и добавить";
  }
});

// ---------- без браузерного меню ----------
// Правый клик оставляем только там, где он нужен: поля ввода и выделенный текст (копировать).
document.addEventListener("contextmenu", (e) => {
  if (e.target.closest("input, textarea") || String(window.getSelection()).trim()) return;
  e.preventDefault();
});

// ---------- уведомления ----------
let notifyOn = true;
function renderBell() {
  const b = $("#bellBtn");
  b.classList.toggle("off", !notifyOn);
  b.innerHTML = notifyOn ? "🔔 Уведомления включены" : "🔕 Уведомления выключены";
}
$("#bellBtn").addEventListener("click", async () => {
  try {
    const s = await api("/api/settings", { method: "POST", body: JSON.stringify({ notifications: !notifyOn }) });
    notifyOn = s.notifications;
    renderBell();
    toast(notifyOn ? "Уведомления о новых письмах включены" : "Уведомления выключены");
  } catch (err) { toast(err.message, true); }
});

// Окно перед глазами — сообщаем программе, чтобы сбросила счётчик «новых».
const ack = () => { if (document.hasFocus()) api("/api/ack", { method: "POST" }).catch(() => {}); };
window.addEventListener("focus", ack);
document.addEventListener("visibilitychange", () => { if (!document.hidden) ack(); });

// ---------- lifecycle ----------
let mailVersion = null;
async function ping() {
  try {
    const r = await api("/api/ping", { method: "POST" });
    if (r.notifications !== notifyOn) { notifyOn = r.notifications; renderBell(); }
    if (mailVersion !== null && r.v !== mailVersion && !state.loading) loadList({ keep: true });
    mailVersion = r.v;
  } catch {}
}
renderBell();
ping();
setInterval(ping, 20000);
setTimeout(ack, 1500);
window.addEventListener("pagehide", () => navigator.sendBeacon("/api/bye?t=" + enc(T)));
setInterval(() => loadList({ keep: true }), 3 * 60 * 1000);

renderFolders();
Promise.all([loadAccounts(), loadSettings()]).then(() => loadList());
