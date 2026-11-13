"""Bounded normal Move_Forward while already flying; fixed Axis1D=1, then cancel."""
import ctypes as C
from .native_macro_cycle import MacroCyclePacket,NativeObjectRef,_make_action_packet,_unsigned
from .reflection_fields import ReflectionError

class FlightForwardPacket(MacroCyclePacket):
    _fields_=[('movement_component',NativeObjectRef),('execute',C.c_uint32),('hold_ms',C.c_uint32),
              ('stage',C.c_uint32),('reserved',C.c_uint32)]

def make_flight_forward_packet(bindings,state,*,session,hold_ms=1000,execute=False):
    if type(execute) is not bool or type(hold_ms) is not int or not 50<=hold_ms<=1500:
        raise ReflectionError('Duracao de W mantido invalida.')
    pawn=bindings['frame_objects']['pawn'];movement=state.get('movement',{})
    if (state.get('session')!=session or tuple(state.get('player_identity',()))!=(pawn['index'],pawn['serial'])
        or movement.get('class')!='AionCharMovementComponent'
        or type(movement.get('MovementMode')) is not int or movement['MovementMode']!=5
        or type(movement.get('CustomMovementMode')) is not int or movement['CustomMovementMode']!=0):
        raise ReflectionError('W aereo exige voo atual e identidade validada.')
    identity=movement.get('identity')
    if not isinstance(identity,(list,tuple)) or len(identity)!=2:
        raise ReflectionError('Componente sem identidade.')
    base=_make_action_packet(bindings,session=session,execute=False,next_target=False,axis='Move_Forward')
    packet=FlightForwardPacket();C.memmove(C.byref(packet),C.byref(base),C.sizeof(base))
    packet.movement_component=NativeObjectRef(_unsigned(movement['address'],64,'Componente',minimum=65536),
        _unsigned(identity[0],32,'Indice'),_unsigned(identity[1],32,'Serial'))
    packet.execute=int(execute);packet.hold_ms=hold_ms
    return packet
