"""Иконка в трее: меню, уведомления Windows, счётчик новых писем на панели задач.

Всё, что касается окон Windows, выполняется в одном потоке (run() с циклом
сообщений). Другие потоки только ставят задачи в очередь через call().
"""
import ctypes
import ctypes.wintypes as wt
import logging
import queue

import icons
import taskbar
import winapi

log = logging.getLogger("oneinbox")

_user32 = ctypes.windll.user32
_shell32 = ctypes.windll.shell32
_kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", winapi.GUID), ("hBalloonIcon", wt.HICON)]


_user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.DefWindowProcW.restype = LRESULT
_user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
_user32.RegisterClassW.restype = wt.ATOM
_user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
_user32.CreateWindowExW.restype = wt.HWND
_user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
_user32.GetMessageW.restype = wt.BOOL
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
_user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
_user32.RegisterWindowMessageW.restype = wt.UINT
_user32.CreatePopupMenu.restype = wt.HMENU
_user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
_user32.SetMenuDefaultItem.argtypes = [wt.HMENU, wt.UINT, wt.UINT]
_user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, ctypes.c_void_p]
_user32.TrackPopupMenu.restype = ctypes.c_int
_user32.DestroyMenu.argtypes = [wt.HMENU]
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
_shell32.Shell_NotifyIconW.restype = wt.BOOL
_kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wt.HMODULE

WM_DESTROY, WM_NULL = 0x0002, 0x0000
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_TRAY, WM_TASK = 0x8001, 0x8002  # WM_APP + n
NIN_BALLOONUSERCLICK = 0x0405
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x10, 0x80
NIIF_USER, NIIF_LARGE_ICON = 0x4, 0x20
MF_STRING, MF_CHECKED, MF_SEPARATOR = 0x0, 0x8, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100
CMD_OPEN, CMD_NOTIFY, CMD_EXIT = 1, 2, 3


class Tray:
    def __init__(self, on_open, on_exit, is_notify_on, toggle_notify):
        self.on_open = on_open
        self.on_exit = on_exit
        self.is_notify_on = is_notify_on
        self.toggle_notify = toggle_notify
        self.q = queue.Queue()
        self.hwnd = None
        self.new = 0
        self.unread = 0
        self.overlay = None
        self._icons = {}
        self._badges = {}
        self._wndproc = WNDPROC(self._proc)  # держим ссылку, иначе GC

    # --- вызовы из любых потоков -----------------------------------------
    def call(self, fn):
        self.q.put(fn)
        if self.hwnd:
            _user32.PostMessageW(self.hwnd, WM_TASK, 0, 0)

    def update(self, new, unread):
        self.call(lambda: self._apply(new, unread))

    def refresh(self):
        self.call(lambda: self._apply(self.new, self.unread))

    def notify(self, title, text):
        self.call(lambda: self._balloon(title, text))

    # --- поток трея -------------------------------------------------------
    def run(self):
        winapi.ole32.CoInitialize(None)
        hinst = _kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=hinst, lpszClassName="OneInboxTray")
        _user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = _user32.CreateWindowExW(0, "OneInboxTray", "One Inbox", 0, 0, 0, 0, 0,
                                            None, None, hinst, None)
        self.taskbar_created = _user32.RegisterWindowMessageW("TaskbarCreated")
        try:
            self.overlay = taskbar.Overlay()
        except Exception:
            log.exception("Значок на панели задач недоступен")
        self._add()
        self._drain()
        msg = wt.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def remove(self):
        _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))

    def _icon(self, kind):
        if kind not in self._icons:
            if kind == "big":
                size = _user32.GetSystemMetrics(11)  # SM_CXICON
                px = icons.render(size, icons.envelope)
            else:
                size = _user32.GetSystemMetrics(49)  # SM_CXSMICON
                px = icons.render(size, icons.envelope_dot if kind == "dot" else icons.envelope)
            self._icons[kind] = icons.to_hicon(size, px)
        return self._icons[kind]

    def _badge(self, n):
        n = min(n, 100)
        if n not in self._badges:
            size = _user32.GetSystemMetrics(49)
            self._badges[n] = icons.to_hicon(size, icons.badge(size, n))
        return self._badges[n]

    def _tip(self):
        t = "Почта"
        parts = []
        if self.new:
            parts.append(f"{self.new} новых")
        if self.unread:
            parts.append(f"{self.unread} непрочитанных")
        return (t + (" — " + ", ".join(parts) if parts else ""))[:127]

    def _nid(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAY
        if flags & NIF_ICON:
            nid.hIcon = self._icon("dot" if self.new else "plain")
        if flags & NIF_TIP:
            nid.szTip = self._tip()
        return nid

    def _add(self):
        ok = _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)))
        log.info("Иконка в трее: %s", "добавлена" if ok else "не добавилась")

    def _apply(self, new, unread):
        self.new, self.unread = new, unread
        _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid(NIF_ICON | NIF_TIP)))
        if self.overlay:
            hwnd = taskbar.find_app_window()
            if hwnd:
                try:
                    self.overlay.set(hwnd, self._badge(new) if new else None,
                                     f"{new} новых писем" if new else "")
                except Exception:
                    log.exception("Не удалось обновить значок на панели задач")

    def _balloon(self, title, text):
        nid = self._nid(NIF_INFO)
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = NIIF_USER | NIIF_LARGE_ICON
        nid.hBalloonIcon = self._icon("big")
        ok = _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        log.info("Уведомление «%s»: %s", title, "показано" if ok else "не показалось")

    def _drain(self):
        while True:
            try:
                fn = self.q.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                log.exception("Ошибка задачи трея")

    def _menu(self):
        m = _user32.CreatePopupMenu()
        _user32.AppendMenuW(m, MF_STRING, CMD_OPEN, "Открыть почту")
        _user32.AppendMenuW(m, MF_STRING | (MF_CHECKED if self.is_notify_on() else 0), CMD_NOTIFY,
                            "Уведомления о новых письмах")
        _user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        _user32.AppendMenuW(m, MF_STRING, CMD_EXIT, "Выход")
        _user32.SetMenuDefaultItem(m, CMD_OPEN, 0)
        pt = wt.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        _user32.SetForegroundWindow(self.hwnd)  # иначе меню не закроется кликом мимо
        cmd = _user32.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None)
        _user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        _user32.DestroyMenu(m)
        if cmd == CMD_OPEN:
            self.on_open()
        elif cmd == CMD_NOTIFY:
            self.toggle_notify()
        elif cmd == CMD_EXIT:
            self.remove()
            self.on_exit()

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                ev = lparam & 0xFFFF
                if ev in (WM_LBUTTONUP, NIN_BALLOONUSERCLICK):
                    self.on_open()
                elif ev == WM_RBUTTONUP:
                    self._menu()
                return 0
            if msg == WM_TASK:
                self._drain()
                return 0
            if msg == getattr(self, "taskbar_created", None):  # проводник перезапустился
                self._add()
                self._apply(self.new, self.unread)
                return 0
        except Exception:
            log.exception("Ошибка в обработчике трея")
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
