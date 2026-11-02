"""Создаёт icon.ico и ярлыки «Почта» на рабочем столе и в меню «Пуск».

Запуск: python install_shortcut.py  (повторный запуск просто пересоздаёт ярлыки)
"""
import ctypes
import ctypes.wintypes as wt
import os
import shutil
import sys

import icons
import shortcut
import taskbar

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "icon.ico")


def desktop_dir():
    buf = ctypes.create_unicode_buffer(wt.MAX_PATH)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf)  # CSIDL_DESKTOPDIRECTORY
    return buf.value


def main():
    icons.write_ico(ICON)
    print("Иконка:", ICON)
    pythonw = shutil.which("pythonw") or os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    args = f'"{os.path.join(HERE, "app.py")}"'
    for folder in (desktop_dir(),
                   os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs")):
        lnk = os.path.join(folder, "Почта.lnk")
        shortcut.create_shortcut(lnk, pythonw, args, HERE, ICON,
                                 "Все почтовые ящики в одном окне", taskbar.APP_ID)
        print("Ярлык:", lnk)


if __name__ == "__main__":
    main()
