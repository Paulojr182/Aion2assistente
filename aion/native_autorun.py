"""One normal Move_AutoForward (auto-run toggle) action; never a field write."""
import ctypes as C
from .native_macro_cycle import MacroCyclePacket, _make_action_packet
from .reflection_fields import ReflectionError


class AutorunPacket(MacroCyclePacket):
    _fields_ = [('execute', C.c_uint32), ('stage', C.c_uint32), ('phase', C.c_uint32), ('reserved', C.c_uint32)]


def make_autorun_packet(bindings, *, session, execute=False, button='Move_AutoForward', release=False):
    if type(execute) is not bool:
        raise ReflectionError('Modo de execução inválido.')
    if release and button not in ('Move_Sprint', 'Move_FlyUp', 'Move_FlyDown', 'QuickSlot_Skill'):
        raise ReflectionError('Só Move_Sprint tem fase de soltar.')
    base = _make_action_packet(bindings, session=session, execute=False, next_target=False, button=button)
    packet = AutorunPacket()
    C.memmove(C.byref(packet), C.byref(base), C.sizeof(base))
    packet.execute = int(execute)
    packet.phase = int(release is True)
    return packet
