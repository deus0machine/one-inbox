"""Шифрование паролей через Windows DPAPI.

Зашифрованное значение может расшифровать только твоя учётка Windows
на этом компьютере. Никаких сторонних библиотек.
"""
import base64
import ctypes
import ctypes.wintypes as wt


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.windll.crypt32
_kernel32 = ctypes.windll.kernel32

_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), wt.LPCWSTR, ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB),
]
_crypt32.CryptProtectData.restype = wt.BOOL
_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB),
]
_crypt32.CryptUnprotectData.restype = wt.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _in_blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf  # buf держим, чтобы его не собрал GC


def _take(out: DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(text: str) -> str:
    blob, _buf = _in_blob(text.encode("utf-8"))
    out = DATA_BLOB()
    if not _crypt32.CryptProtectData(ctypes.byref(blob), "oneinbox", None, None, None,
                                     _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError()
    return base64.b64encode(_take(out)).decode("ascii")


def unprotect(token: str) -> str:
    blob, _buf = _in_blob(base64.b64decode(token))
    out = DATA_BLOB()
    if not _crypt32.CryptUnprotectData(ctypes.byref(blob), None, None, None, None,
                                       _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError()
    return _take(out).decode("utf-8")
