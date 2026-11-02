"""Окно программы на панели задач: своя группа и иконка, поиск окна, бейдж с числом.

Windows группирует окна по AppUserModelID. Ставим окну Edge собственный ID,
иконку и команду перезапуска — тогда на панели задач это отдельное приложение
«Почта» с конвертом, а закреплённая кнопка запускает One Inbox, а не Edge.
"""
import ctypes
import ctypes.wintypes as wt
import re
import time

import winapi

APP_ID = "OneInbox.Pochta"
# Заголовок окна-приложения Edge — ровно заголовок страницы: «Почта» или «(12) Почта».
# У обычных окон браузера в конце есть « - Microsoft Edge», так что их не спутать.
TITLE_RE = re.compile(r"^(\(\d+\) )?Почта$")

_user32 = ctypes.windll.user32
_shell32 = ctypes.windll.shell32

_shell32.SHGetPropertyStoreForWindow.argtypes = [wt.HWND, ctypes.POINTER(winapi.GUID),
                                                 ctypes.POINTER(ctypes.c_void_p)]
_shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
_user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
_user32.LoadImageW.restype = wt.HANDLE
_user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.SendMessageW.restype = wt.LPARAM
_user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.IsWindowVisible.argtypes = [wt.HWND]
_user32.IsIconic.argtypes = [wt.HWND]
_user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.GetForegroundWindow.restype = wt.HWND
_user32.IsWindow.argtypes = [wt.HWND]


def _window_store(hwnd):
    ps = ctypes.c_void_p()
    hr = _shell32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(winapi.guid(winapi.IID_IPropertyStore)),
                                              ctypes.byref(ps))
    return ps if hr == 0 else None


def set_window_identity(hwnd, relaunch_cmd, icon_path, name="Почта"):
    ps = _window_store(hwnd)
    if not ps:
        return False
    try:
        # По документации команды перезапуска задаются до самого ID.
        winapi.store_set_strings(ps, [
            (winapi.PID_RELAUNCH_COMMAND, relaunch_cmd),
            (winapi.PID_RELAUNCH_ICON, icon_path + ",0"),
            (winapi.PID_RELAUNCH_NAME, name),
            (winapi.PID_APP_ID, APP_ID),
        ])
    finally:
        winapi.release(ps)
    return True


def get_window_app_id(hwnd):
    ps = _window_store(hwnd)
    if not ps:
        return None
    try:
        return winapi.store_get_string(ps, winapi.PID_APP_ID)
    finally:
        winapi.release(ps)


def set_window_icon(hwnd, icon_path):
    for which, size in ((0, 16), (1, 32), (1, 48)):  # ICON_SMALL / ICON_BIG
        h = _user32.LoadImageW(None, icon_path, 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        if h:
            _user32.SendMessageW(hwnd, 0x80, which, h)  # WM_SETICON


def find_app_window():
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(64)
        _user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != "Chrome_WidgetWin_1":
            return True
        title = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, title, 256)
        if TITLE_RE.match(title.value):
            found.append(hwnd)
            return False
        return True

    _user32.EnumWindows(cb, 0)
    return found[0] if found else None


def bring_to_front(hwnd):
    _user32.ShowWindow(hwnd, 9 if _user32.IsIconic(hwnd) else 5)  # SW_RESTORE / SW_SHOW
    _user32.SetForegroundWindow(hwnd)


def close_window(hwnd):
    _user32.PostMessageW(hwnd, 0x10, 0, 0)  # WM_CLOSE


def is_foreground():
    fg = _user32.GetForegroundWindow()
    return bool(fg) and fg == find_app_window()


def brand_window(relaunch_cmd, icon_path, timeout=30):
    """Ждёт окно программы и ставит ему свою иконку и группу на панели задач."""
    winapi.ole32.CoInitialize(None)
    deadline = time.time() + timeout
    hwnd = None
    while time.time() < deadline and not hwnd:
        hwnd = find_app_window()
        if not hwnd:
            time.sleep(0.3)
    if not hwnd:
        return None
    set_window_identity(hwnd, relaunch_cmd, icon_path)
    set_window_icon(hwnd, icon_path)
    time.sleep(2)  # Edge может перерисовать иконку после загрузки страницы — повторяем
    if _user32.IsWindow(hwnd):
        set_window_icon(hwnd, icon_path)
    return hwnd


class Overlay:
    """Значок поверх иконки на панели задач (ITaskbarList3::SetOverlayIcon)."""
    CLSID_TaskbarList = "56FDF344-FD6D-11d0-958A-006097C9A090"
    IID_ITaskbarList3 = "EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF"

    def __init__(self):
        self.ptr = winapi.co_create(self.CLSID_TaskbarList, self.IID_ITaskbarList3)
        winapi.check(winapi.vcall(self.ptr, 3, []), "HrInit")

    def set(self, hwnd, hicon, description=""):
        winapi.vcall(self.ptr, 18, [wt.HWND, wt.HICON, wt.LPCWSTR], hwnd, hicon, description)
