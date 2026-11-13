"""One call to the game's own map auto-move (MoveToLocation) toward a goal point."""
import ctypes as C
import math
from .native_macro_cycle import MacroCyclePacket, make_collect_packet
from .reflection_fields import ReflectionError


class AutoMovePacket(MacroCyclePacket):
    _fields_ = [('goal', C.c_double * 3), ('execute', C.c_uint32), ('stage', C.c_uint32),
                ('map_mode', C.c_uint32), ('mode_before', C.c_uint32),
                ('plan', C.c_uint32), ('point_count', C.c_uint32), ('path_object', C.c_uint64),
                ('points', (C.c_double * 3) * 64),
                ('flag_offset', C.c_uint32), ('flag_mask', C.c_uint8), ('flag_value', C.c_uint8),
                ('flag_enable', C.c_uint8), ('flag_only', C.c_uint8), ('flag_before', C.c_uint8),
                ('flag_after', C.c_uint8), ('flag_pad', C.c_uint8 * 6)]


def make_auto_move_packet(bindings, *, session, goal, execute=False, map_mode=False, plan=False, flag=None, flag_only=False, map_position=False, map_stop=False, map_flight=False, map_manager=False):
    if type(execute) is not bool:
        raise ReflectionError('Modo de execução inválido.')
    if (not isinstance(goal, (tuple, list)) or len(goal) != 3
            or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e7 for v in goal)):
        raise ReflectionError('Destino de deslocamento inválido.')
    if type(map_position) is not bool or (map_position and (map_mode or plan or flag is not None or flag_only)):
        raise ReflectionError('Pedido de mapa exige modo isolado.')
    if type(map_stop) is not bool or (map_stop and (map_position or map_mode or plan or flag is not None or flag_only)):
        raise ReflectionError('Parada de mapa exige modo isolado.')
    if type(map_flight) is not bool or (map_flight and not map_position):
        raise ReflectionError('Pedido aereo exige destino posicional.')
    if type(map_manager) is not bool or (map_manager and not (map_position and map_flight)):
        raise ReflectionError('Gerenciador aereo exige pedido posicional de voo.')
    base = make_collect_packet(bindings, session=session)
    packet = AutoMovePacket()
    C.memmove(C.byref(packet), C.byref(base), C.sizeof(base))
    packet.goal[:] = [float(v) for v in goal]
    packet.execute = int(execute)
    packet.map_mode = 5 if map_manager else (3 if map_stop else (4 if map_flight else (2 if map_position else int(map_mode is True))))
    packet.plan = int(plan is True)
    if flag is not None:
        offset, mask, value = flag
        if (type(offset) is not int or not 0x30 <= offset < 0x10000 or type(mask) is not int
                or mask not in (1, 2, 4, 8, 16, 32, 64, 128) or value not in (0, 1)):
            raise ReflectionError('Opção de movimento inválida.')
        packet.flag_offset, packet.flag_mask, packet.flag_value, packet.flag_enable = offset, mask, value, 1
        packet.flag_only = int(flag_only is True)
    elif flag_only:
        raise ReflectionError('flag_only exige uma opção.')
    if plan and map_mode:
        raise ReflectionError('Planejamento não grava modo.')
    return packet
