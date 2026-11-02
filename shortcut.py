"""Создание ярлыков .lnk с AppUserModelID (через COM, без PowerShell).

AppUserModelID в ярлыке в меню «Пуск» нужен Windows, чтобы уведомления
подписывались «Почта» с нашей иконкой, а закреплённое окно совпадало с ярлыком.
"""
import ctypes
import ctypes.wintypes as wt
import os

import winapi

CLSID_ShellLink = "00021401-0000-0000-C000-000000000046"
IID_IShellLinkW = "000214F9-0000-0000-C000-000000000046"
IID_IPersistFile = "0000010B-0000-0000-C000-000000000046"


def create_shortcut(lnk_path, target, args, workdir, icon, description, app_id=None):
    winapi.ole32.CoInitialize(None)
    link = winapi.co_create(CLSID_ShellLink, IID_IShellLinkW)
    try:
        S = [wt.LPCWSTR]
        winapi.check(winapi.vcall(link, 20, S, target), "SetPath")
        winapi.check(winapi.vcall(link, 11, S, args), "SetArguments")
        winapi.check(winapi.vcall(link, 9, S, workdir), "SetWorkingDirectory")
        winapi.check(winapi.vcall(link, 17, [wt.LPCWSTR, ctypes.c_int], icon, 0), "SetIconLocation")
        winapi.check(winapi.vcall(link, 7, S, description), "SetDescription")
        if app_id:
            store = winapi.qi(link, winapi.IID_IPropertyStore)
            try:
                winapi.store_set_strings(store, [(winapi.PID_APP_ID, app_id)])
            finally:
                winapi.release(store)
        pf = winapi.qi(link, IID_IPersistFile)
        try:
            os.makedirs(os.path.dirname(lnk_path), exist_ok=True)
            winapi.check(winapi.vcall(pf, 6, [wt.LPCWSTR, wt.BOOL], lnk_path, True), "Save")
        finally:
            winapi.release(pf)
    finally:
        winapi.release(link)


def read_app_id(lnk_path):
    """Для проверки: AppUserModelID, записанный в ярлык."""
    winapi.ole32.CoInitialize(None)
    link = winapi.co_create(CLSID_ShellLink, IID_IShellLinkW)
    try:
        pf = winapi.qi(link, IID_IPersistFile)
        try:
            winapi.check(winapi.vcall(pf, 5, [wt.LPCWSTR, wt.DWORD], lnk_path, 0), "Load")
        finally:
            winapi.release(pf)
        store = winapi.qi(link, winapi.IID_IPropertyStore)
        try:
            return winapi.store_get_string(store, winapi.PID_APP_ID)
        finally:
            winapi.release(store)
    finally:
        winapi.release(link)
