"""Windows hotkey on a dedicated message queue."""
import ctypes
from ctypes import wintypes
import queue
import threading


# Windows virtual key codes for punctuation and extended keyboard keys.
EXTRA_KEYS = {
    'PAUSE': 0x13, 'CAPS LOCK': 0x14, 'PRINT SCREEN': 0x2C,
    'NUM *': 0x6A, 'NUM +': 0x6B, 'NUM SEPARADOR': 0x6C,
    'NUM -': 0x6D, 'NUM DECIMAL': 0x6E, 'NUM /': 0x6F,
    'NUM LOCK': 0x90, 'SCROLL LOCK': 0x91,
    'OEM BA': 0xBA, 'OEM BB': 0xBB, 'OEM BC': 0xBC, 'OEM BD': 0xBD,
    'OEM BE': 0xBE, 'OEM BF': 0xBF, 'OEM C0': 0xC0, 'OEM C1': 0xC1,
    'OEM DB': 0xDB, 'OEM DC': 0xDC, 'OEM DD': 0xDD, 'OEM DE': 0xDE,
    'OEM DF': 0xDF, 'OEM E2': 0xE2,
}


def parse_hotkey(value):
    parts = [part.strip().upper() for part in value.split('+')]
    modifiers = {'ALT': 1, 'CTRL': 2, 'SHIFT': 4, 'WIN': 8}
    keys = {chr(code): code for code in range(48, 58)}
    keys.update({chr(code): code for code in range(65, 91)})
    keys.update({f'F{i}': 111+i for i in range(1, 25) if i != 12})
    keys.update({'ESPACO': 32, 'TAB': 9, 'ENTER': 13, 'INSERT': 45,
                 'HOME': 36, 'END': 35, 'PAGEUP': 33, 'PAGEDOWN': 34,
                 'LEFT': 37, 'UP': 38, 'RIGHT': 39, 'DOWN': 40,
                 'DELETE': 46, 'BACKSPACE': 8})
    keys.update({f'NUM{i}': 96+i for i in range(10)})
    # NUM + includes a literal plus and is a standalone key.
    if value.strip().upper() == 'NUM +':
        return 'NUM +', 0, EXTRA_KEYS['NUM +']
    keys.update(EXTRA_KEYS)
    key = parts[-1]
    mods = parts[:-1]
    if (key not in keys or any(part not in modifiers for part in mods)
            or len(set(mods)) != len(mods)):
        raise ValueError('Essa tecla não está disponível como atalho.')
    return '+'.join(parts), sum(modifiers[part] for part in mods), keys[key]


def capture_key(keycode):
    if 65 <= keycode <= 90 or 48 <= keycode <= 57:
        return parse_hotkey(chr(keycode))
    if 112 <= keycode <= 135 and keycode != 123:
        return parse_hotkey(f'F{keycode-111}')
    if 96 <= keycode <= 105:
        return parse_hotkey(f'NUM{keycode-96}')
    named = {32:'ESPACO', 9:'TAB', 13:'ENTER', 45:'INSERT', 36:'HOME',
             35:'END', 33:'PAGEUP', 34:'PAGEDOWN', 37:'LEFT', 38:'UP',
             39:'RIGHT', 40:'DOWN', 46:'DELETE', 8:'BACKSPACE'}
    if keycode in named:
        return parse_hotkey(named[keycode])
    for label, code in EXTRA_KEYS.items():
        if keycode == code:
            return parse_hotkey(label)
    raise ValueError('O Windows não oferece essa tecla como atalho. Tente outra.')


class AttackHotkey:
    def __init__(self, events):
        self.events = events
        self.commands = queue.Queue()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        user.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        user.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                     wintypes.UINT, wintypes.UINT, wintypes.UINT)
        current = None
        identifier = 1
        message = wintypes.MSG()
        try:
            while not self.closed.is_set():
                try:
                    candidate = self.commands.get(timeout=0.04)
                except queue.Empty:
                    pass
                else:
                    if candidate == current:
                        self.events.put(('hotkey_saved', current))
                    elif candidate is None:
                        if current is not None:
                            user.UnregisterHotKey(None, identifier)
                        current = None
                        self.events.put(('hotkey_saved', None))
                    else:
                        name, modifiers, key = candidate
                        new_id = 3 - identifier
                        if user.RegisterHotKey(None, new_id, modifiers | 0x4000, key):
                            if current is not None:
                                user.UnregisterHotKey(None, identifier)
                            current, identifier = candidate, new_id
                            self.events.put(('hotkey_saved', current))
                        else:
                            self.events.put(('hotkey_error', name))
                while user.PeekMessageW(ctypes.byref(message), None, 0x0312, 0x0312, 1):
                    if current is not None and message.wParam == identifier:
                        self.events.put(('hotkey_toggle', None))
        finally:
            if current is not None:
                user.UnregisterHotKey(None, identifier)

    def close(self):
        self.closed.set()
        self.thread.join(timeout=0.3)
