"""Expose the attack state on the Windows taskbar button."""
import ctypes as C
from ctypes import wintypes as W
from uuid import UUID


def set_overlay(window, icon_path=None):
    ole = C.OleDLL('ole32')
    user = C.WinDLL('user32')
    ole.CoInitializeEx.argtypes = (C.c_void_p, W.DWORD)
    ole.CoCreateInstance.argtypes = (C.c_void_p, C.c_void_p, W.DWORD, C.c_void_p, C.c_void_p)
    user.GetAncestor.argtypes = (W.HWND, W.UINT)
    user.GetAncestor.restype = W.HWND
    user.LoadImageW.argtypes = (W.HINSTANCE, W.LPCWSTR, W.UINT, C.c_int, C.c_int, W.UINT)
    user.LoadImageW.restype = W.HANDLE
    user.DestroyIcon.argtypes = (W.HANDLE,)
    clsid = C.create_string_buffer(UUID('56fdf344-fd6d-11d0-958a-006097c9a090').bytes_le)
    iid = C.create_string_buffer(UUID('ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf').bytes_le)
    ole.CoInitializeEx(None, 2)
    taskbar = C.c_void_p()
    icon = None
    try:
        ole.CoCreateInstance(clsid, None, 1, iid, C.byref(taskbar))
        table = C.cast(taskbar, C.POINTER(C.POINTER(C.c_void_p))).contents
        if C.WINFUNCTYPE(C.c_long, C.c_void_p)(table[3])(taskbar) < 0:
            return False
        if icon_path:
            icon = user.LoadImageW(None, str(icon_path), 1, 16, 16, 0x10)
            if not icon:
                return False
        hwnd = user.GetAncestor(window, 2) or window
        overlay = C.WINFUNCTYPE(C.c_long, C.c_void_p, W.HWND, W.HANDLE, W.LPCWSTR)(table[18])
        return overlay(taskbar, hwnd, icon, 'Ataque ativo' if icon else None) >= 0
    finally:
        if icon:
            user.DestroyIcon(icon)
        if taskbar.value:
            C.WINFUNCTYPE(W.ULONG, C.c_void_p)(table[2])(taskbar)
        ole.CoUninitialize()
