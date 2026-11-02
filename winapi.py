"""Минимальные обёртки над COM для ctypes (без сторонних библиотек)."""
import ctypes
import ctypes.wintypes as wt
import uuid

HRESULT = ctypes.c_long

ole32 = ctypes.windll.ole32
ole32.CoInitialize.argtypes = [ctypes.c_void_p]
ole32.CoInitialize.restype = HRESULT


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def guid(s):
    u = uuid.UUID(s)
    g = GUID(u.time_low, u.time_mid, u.time_hi_version)
    g.Data4[:] = list(u.bytes[8:])
    return g


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wt.DWORD)]


class PROPVARIANT(ctypes.Structure):  # только VT_LPWSTR; 24 байта на x64
    _fields_ = [("vt", wt.USHORT), ("r1", wt.USHORT), ("r2", wt.USHORT), ("r3", wt.USHORT),
                ("pwszVal", ctypes.c_wchar_p), ("pad", ctypes.c_void_p)]


VT_LPWSTR = 31
FMTID_AUMID = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"  # System.AppUserModel.*
PID_RELAUNCH_COMMAND, PID_RELAUNCH_ICON, PID_RELAUNCH_NAME, PID_APP_ID = 2, 3, 4, 5
IID_IPropertyStore = "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"

ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wt.DWORD,
                                   ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
ole32.CoCreateInstance.restype = HRESULT


def vcall(ptr, index, argtypes, *args, restype=HRESULT):
    """Вызов метода COM-интерфейса по номеру в таблице виртуальных функций."""
    vtbl = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                       ctypes.POINTER(ctypes.c_void_p))
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(ptr, *args)


def check(hr, what):
    if hr < 0:
        raise OSError(f"{what}: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
    return hr


def co_create(clsid, iid):
    p = ctypes.c_void_p()
    check(ole32.CoCreateInstance(ctypes.byref(guid(clsid)), None, 1,  # CLSCTX_INPROC_SERVER
                                 ctypes.byref(guid(iid)), ctypes.byref(p)), "CoCreateInstance")
    return p


def qi(ptr, iid):
    p = ctypes.c_void_p()
    check(vcall(ptr, 0, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                ctypes.byref(guid(iid)), ctypes.byref(p)), "QueryInterface")
    return p


def release(ptr):
    if ptr:
        vcall(ptr, 2, [], restype=ctypes.c_ulong)


def store_set_strings(store, items):
    """IPropertyStore: записать строки System.AppUserModel.* ((pid, value), …) и сохранить."""
    for pid, val in items:
        key = PROPERTYKEY(guid(FMTID_AUMID), pid)
        pv = PROPVARIANT(vt=VT_LPWSTR, pwszVal=val)
        check(vcall(store, 6, [ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)],
                    ctypes.byref(key), ctypes.byref(pv)), "SetValue")
    check(vcall(store, 7, []), "Commit")


def store_get_string(store, pid):
    key = PROPERTYKEY(guid(FMTID_AUMID), pid)
    pv = PROPVARIANT()
    vcall(store, 5, [ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)],
          ctypes.byref(key), ctypes.byref(pv))
    return pv.pwszVal if pv.vt == VT_LPWSTR else None
