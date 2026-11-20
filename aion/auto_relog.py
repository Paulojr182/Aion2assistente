"""Reentrada automatica apos o jogo mandar para a selecao de servidor por inatividade.

Sequencia: traz o jogo para frente e aperta F tres vezes (Confirmar -> Entrar no servidor
-> Iniciar jogo), esperando o carregamento de cada tela.
"""
import ctypes
from ctypes import wintypes
import time

user32 = ctypes.windll.user32
SCAN_F = 0x21
STEPS = (('Confirmar', 6.0), ('Entrar no servidor', 12.0), ('Iniciar jogo', 30.0))


class KI(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [('ki', KI), ('pad', ctypes.c_byte * 32)]
    _anonymous_ = ('u',)
    _fields_ = [('type', wintypes.DWORD), ('u', _U)]


def find_game_window(pid):
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            rc = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rc))
            found.append(((rc.right - rc.left) * (rc.bottom - rc.top), hwnd))
        return True
    user32.EnumWindows(proc(cb), 0)
    return max(found)[1] if found else None


def press_f(hwnd):
    user32.ShowWindow(hwnd, 9)              # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    for flags in (0x0008, 0x0008 | 0x0002):  # SCANCODE down, SCANCODE|KEYUP
        inp = INPUT(type=1)
        inp.ki = KI(0, SCAN_F, flags, 0, 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.12)


def relog(pid, log=lambda msg: None, cancelled=lambda: False):
    """Executa a sequencia. Devolve False se a janela do jogo nao foi achada ou se foi cancelado."""
    hwnd = find_game_window(pid)
    if not hwnd:
        log('Janela do jogo não encontrada para reentrar.')
        return False
    for name, wait in STEPS:
        if cancelled():
            return False
        log(f'Reentrando: apertando F ({name})…')
        press_f(hwnd)
        end = time.monotonic() + wait
        while time.monotonic() < end:
            if cancelled():
                return False
            time.sleep(0.5)
    return True
