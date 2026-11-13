"""Caminho real do AION2.exe a partir do processo em execucao (cada PC pode ter a Steam em outro lugar)."""
import ctypes
from ctypes import wintypes
from pathlib import Path

DEFAULT = Path(r'C:\Program Files (x86)\Steam\steamapps\common\AION2\Aion2\Binaries\Win64\AION2.exe')


def image_path(pid):
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    h = k.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return DEFAULT
    try:
        buf = ctypes.create_unicode_buffer(32768); size = wintypes.DWORD(len(buf))
        if k.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf, ctypes.byref(size)):
            p = Path(buf.value)
            if p.is_file():
                return p
    finally:
        k.CloseHandle(ctypes.c_void_p(h))
    return DEFAULT


def game_offset(name, fallback):
    """Endereco gravado pela sincronizacao em native/game_offsets.txt (mesmo arquivo lido pelas DLLs)."""
    path = Path(__file__).resolve().parent.parent / 'native' / 'game_offsets.txt'
    try:
        for line in path.read_text('ascii').splitlines():
            key, _, value = line.partition('=')
            if key.strip() == name and value.strip().isdigit():
                return int(value)
    except OSError:
        pass
    return fallback
