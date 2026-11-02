"""Иконки, нарисованные кодом: конверт, конверт с красной точкой, кружок с числом.

Умеет отдавать готовый HICON для трея/панели задач и собирать .ico-файл.
"""
import ctypes
import ctypes.wintypes as wt
import struct

BLUE = (0x3E, 0x63, 0xDD)
WHITE = (0xFF, 0xFF, 0xFF)
RED = (0xE5, 0x48, 0x4D)

# Пиксельный шрифт 3×5 для цифр на бейдже
FONT = {
    "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
    "+": ["000", "010", "111", "010", "000"],
}


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def _in_rrect(x, y, x0, y0, x1, y1, r):
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def envelope(x, y):
    """Цвет точки (x, y ∈ [0,1]) или None (прозрачно)."""
    if not _in_rrect(x, y, 0.06, 0.18, 0.94, 0.82, 0.12):
        return None
    th = 0.05
    if _seg_dist(x, y, 0.17, 0.31, 0.5, 0.56) < th or _seg_dist(x, y, 0.5, 0.56, 0.83, 0.31) < th:
        return WHITE
    return BLUE


def envelope_dot(x, y):
    """Конверт с красной точкой «есть новые» в правом нижнем углу."""
    d = ((x - 0.76) ** 2 + (y - 0.74) ** 2) ** 0.5
    if d < 0.2:
        return RED
    if d < 0.27:
        return WHITE
    return envelope(x, y)


def _pill(x, y):
    return RED if _in_rrect(x, y, 0.0, 0.0, 1.0, 1.0, 0.3) else None


def render(size, sampler, ss=4):
    """Пиксели size×size в формате BGRA, строки сверху вниз (bytearray)."""
    out = bytearray(size * size * 4)
    n = ss * ss
    for py in range(size):
        for px in range(size):
            r = g = b = a = 0
            for sy in range(ss):
                for sx in range(ss):
                    c = sampler((px + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size)
                    if c:
                        r += c[0]; g += c[1]; b += c[2]; a += 1
            if a:
                i = (py * size + px) * 4
                out[i:i + 4] = bytes((b // a, g // a, r // a, round(255 * a / n)))
    return out


def badge(size, number):
    """Красный скруглённый квадрат с числом (для значка на панели задач)."""
    text = str(number) if number <= 99 else "99+"
    px = render(size, _pill)
    s = max(1, min(int(size * 0.65) // 5, int(size * 0.88) // (4 * len(text) - 1)))
    w, h = s * (4 * len(text) - 1), 5 * s
    x0, y0 = (size - w) // 2, (size - h) // 2
    for ci, ch in enumerate(text):
        for row, bits in enumerate(FONT[ch]):
            for col, bit in enumerate(bits):
                if bit == "1":
                    for dy in range(s):
                        for dx in range(s):
                            x = x0 + (ci * 4 + col) * s + dx
                            y = y0 + row * s + dy
                            if 0 <= x < size and 0 <= y < size:
                                i = (y * size + x) * 4
                                px[i:i + 4] = b"\xff\xff\xff\xff"
    return px


# --- HICON -----------------------------------------------------------------
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]


_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_user32.GetDC.argtypes = [wt.HWND]
_user32.GetDC.restype = wt.HDC
_user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
_gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFOHEADER), wt.UINT,
                                    ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
_gdi32.CreateDIBSection.restype = wt.HBITMAP
_gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT, ctypes.c_void_p]
_gdi32.CreateBitmap.restype = wt.HBITMAP
_gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
_user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
_user32.CreateIconIndirect.restype = wt.HICON
_user32.DestroyIcon.argtypes = [wt.HICON]


def to_hicon(size, bgra):
    bi = BITMAPINFOHEADER(40, size, -size, 1, 32, 0, 0, 0, 0, 0, 0)  # -size: строки сверху вниз
    bits = ctypes.c_void_p()
    hdc = _user32.GetDC(None)
    hbm = _gdi32.CreateDIBSection(hdc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
    _user32.ReleaseDC(None, hdc)
    if not hbm:
        raise ctypes.WinError()
    ctypes.memmove(bits, bytes(bgra), len(bgra))
    mask_bits = ctypes.create_string_buffer(((size + 15) // 16) * 2 * size)
    hmask = _gdi32.CreateBitmap(size, size, 1, 1, mask_bits)
    hicon = _user32.CreateIconIndirect(ctypes.byref(ICONINFO(True, 0, 0, hmask, hbm)))
    _gdi32.DeleteObject(hbm)
    _gdi32.DeleteObject(hmask)
    if not hicon:
        raise ctypes.WinError()
    return hicon


def destroy(hicon):
    if hicon:
        _user32.DestroyIcon(hicon)


# --- .ico-файл -------------------------------------------------------------
def write_ico(path, sampler=envelope, sizes=(16, 20, 24, 32, 40, 48, 64, 256)):
    images = []
    for s in sizes:
        px = render(s, sampler, ss=4 if s <= 64 else 2)
        rows = [bytes(px[y * s * 4:(y + 1) * s * 4]) for y in range(s)]
        pixels = b"".join(reversed(rows))  # в .ico строки снизу вверх
        header = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
        images.append(header + pixels + b"\0" * (((s + 31) // 32) * 4 * s))
    out = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    for s, img in zip(sizes, images):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    with open(path, "wb") as f:
        f.write(out + b"".join(images))
