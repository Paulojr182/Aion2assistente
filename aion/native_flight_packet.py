"""Explicit one-shot normal Move_Fly action; never a raw movement-mode setter."""
import ctypes as C
import math
from .native_movement import MovementPacket,make_movement_packet
from .native_macro_cycle import MacroCyclePacket,NativeObjectRef,_make_action_packet,_unsigned
from .reflection_fields import ReflectionError


class FlightTogglePacket(MacroCyclePacket):
    _fields_=[('movement_component',NativeObjectRef),('execute',C.c_uint32),('movement_mode_before',C.c_uint32)]


def make_flight_toggle_packet(bindings,profile,*,session,execute=False,land=False):
    movement=profile.get('movement')
    need=5 if land else 1
    if (type(execute) is not bool or profile.get('session')!=session or not isinstance(movement,dict)
            or movement.get('class')!='AionCharMovementComponent'
            or type(movement.get('MovementMode')) is not int or type(movement.get('CustomMovementMode')) is not int
            or movement.get('MovementMode')!=need or movement.get('CustomMovementMode')!=0
            or tuple(profile.get('player_identity') or ())!=(bindings['frame_objects']['pawn']['index'],bindings['frame_objects']['pawn']['serial'])):
        raise ReflectionError('Voo exige personagem/sessão atual em modo terrestre validado.')
    base=_make_action_packet(bindings,session=session,execute=False,next_target=False,flight=True)
    packet=FlightTogglePacket();C.memmove(C.byref(packet),C.byref(base),C.sizeof(base))
    identity=movement.get('identity')
    if not isinstance(identity,(tuple,list)) or len(identity)!=2:raise ReflectionError('Componente de movimento sem identidade.')
    packet.movement_component=NativeObjectRef(_unsigned(movement['address'],64,'Componente',minimum=65536),
        _unsigned(identity[0],32,'Índice'),_unsigned(identity[1],32,'Serial'))
    packet.execute=(2 if land else 1) if execute else 0
    return packet


class FlightMovementPacket(MovementPacket):
    _fields_=[('movement_component',NativeObjectRef)]


def make_flight_movement_packet(bindings,movement_profile,flight_state,*,session,direction,
                                duration_ms=100,execute=False,stream=False,stream_stop=False):
    if type(stream) is not bool or type(stream_stop) is not bool or (stream_stop and not stream):
        raise ReflectionError('Modo de fluxo aereo invalido.')
    movement=flight_state.get('movement')
    if (type(execute) is not bool or flight_state.get('session')!=session or not isinstance(movement,dict)
            or movement.get('class')!='AionCharMovementComponent'
            or type(movement.get('MovementMode')) is not int or (not stream_stop and movement['MovementMode']!=5)
            or type(movement.get('CustomMovementMode')) is not int or movement['CustomMovementMode']!=0
            or tuple(flight_state.get('player_identity') or ())!=(bindings['frame_objects']['pawn']['index'],bindings['frame_objects']['pawn']['serial'])):
        raise ReflectionError('Movimento 3D exige voo atual no mesmo personagem/sessão.')
    if (not isinstance(direction,(tuple,list)) or len(direction)!=3
            or any(type(v) not in (int,float) or not math.isfinite(v) for v in direction)
            or sum(v*v for v in direction)>1.000001 or execute and not stream_stop and sum(v*v for v in direction)<.000001):
        raise ReflectionError('Direção 3D inválida.')
    base=make_movement_packet(bindings,movement_profile,session=session,duration_ms=duration_ms,
                              direction=(0.,0.,0.),execute=False)
    packet=FlightMovementPacket();C.memmove(C.byref(packet),C.byref(base),C.sizeof(base))
    identity=movement.get('identity')
    if not isinstance(identity,(tuple,list)) or len(identity)!=2:raise ReflectionError('Componente de voo sem identidade.')
    packet.movement_component=NativeObjectRef(_unsigned(movement['address'],64,'Componente',minimum=65536),
        _unsigned(identity[0],32,'Índice'),_unsigned(identity[1],32,'Serial'))
    packet.direction[:]=direction
    packet.execute=(3 if stream_stop else 2) if stream and execute else int(execute)
    return packet
