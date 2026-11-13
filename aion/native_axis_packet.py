"""Explicit normal Move_Forward/Move_Right axis delivery packet; never a raw movement-mode setter."""
import ctypes as C
import math
from .native_macro_cycle import MacroCyclePacket, _make_action_packet, AXIS_IDS
from .reflection_fields import ReflectionError


class AxisPacket(MacroCyclePacket):
    _fields_ = [('value', C.c_double), ('execute', C.c_uint32), ('stop', C.c_uint32)]


def make_axis_packet(bindings, *, session, axis, value, execute=False, stop=False):
    if axis not in AXIS_IDS:
        raise ReflectionError('Eixo fora do conjunto revisado.')
    if (type(execute) is not bool or type(stop) is not bool or (stop and value != 0) or type(value) not in (int, float) or not math.isfinite(value)
            or not -1.0 <= value <= 1.0):
        raise ReflectionError('Valor de eixo inválido.')
    base = _make_action_packet(bindings, session=session, execute=False, next_target=False, axis=axis)
    packet = AxisPacket()
    C.memmove(C.byref(packet), C.byref(base), C.sizeof(base))
    packet.value = float(value)
    packet.execute = int(execute)
    packet.stop = int(stop)
    return packet
