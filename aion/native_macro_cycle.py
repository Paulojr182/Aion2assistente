"""Build an explicit, guarded Macro lifecycle probe from live native bindings."""
import ctypes as C
from .background_probe import ProbePacket
from .reflection_fields import ReflectionError


class NativeObjectRef(C.Structure):
    _fields_=[('address',C.c_uint64),('index',C.c_uint32),('serial',C.c_uint32)]


class NativeDelta(C.Structure):
    _fields_=[('offset',C.c_uint32),('before',C.c_uint8),('active',C.c_uint8),('after',C.c_uint8),('reserved',C.c_uint8)]


class MacroCyclePacket(ProbePacket):
    _fields_=[('pool_rva',C.c_uint64),('registry_rva',C.c_uint64),('global_rva',C.c_uint64),
        ('pawn',NativeObjectRef),('controller',NativeObjectRef),('component',NativeObjectRef),('input',NativeObjectRef),('action',NativeObjectRef),
        ('start_binding',C.c_uint64),('start_shared',C.c_uint64),('start_delegate',C.c_uint64),
        ('stop_binding',C.c_uint64),('stop_shared',C.c_uint64),('stop_delegate',C.c_uint64),
        ('method_rva',C.c_uint64),('callback_rva',C.c_uint64),('pawn_controller_offset',C.c_uint32),
        ('controller_input_offset',C.c_uint32),('controller_component_offset',C.c_uint32),
        ('capture_size',C.c_uint32),('mode',C.c_uint32),('calls',C.c_uint32),('diff_count',C.c_uint32),('failure_stage',C.c_uint32),
        ('differences',NativeDelta*64),('macro_state_mask',C.c_uint32),
        ('macro_before',C.c_uint32),('macro_active',C.c_uint32),('macro_after',C.c_uint32)]


def _unsigned(value, bits, label, *, minimum=0):
    # ctypes silently wraps negative and oversized integers. Reject them before
    # building a frame that could otherwise refer to a different native object.
    if type(value) is not int or not minimum <= value < (1 << bits):
        raise ReflectionError(f'{label} fora do limite nativo.')
    return value


def make_macro_cycle_packet(observation,*,session,execute=False):
    return _make_action_packet(observation,session=session,execute=execute,next_target=False)


def make_next_target_packet(observation,*,session,execute=False):
    return _make_action_packet(observation,session=session,execute=execute,next_target=True)


def make_collect_packet(observation,*,session):
    return _make_action_packet(observation,session=session,execute=True,next_target=False,collect=True)


def make_macro_lease_packet(observation,*,session,execute=False):
    packet=make_macro_cycle_packet(observation,session=session,execute=execute)
    if execute:packet.mode=4
    return packet


def make_macro_lease_control_packet(observation,*,session,operation):
    if operation not in ('renew','stop','stop_current'):
        raise ReflectionError('Operação de período do macro inválida.')
    packet=make_macro_cycle_packet(observation,session=session)
    packet.mode={'renew':5,'stop':6,'stop_current':7}[operation]
    return packet


AXIS_IDS={'Move_Forward':1,'Move_Right':2,'Camera_RotateX':None,'Camera_RotateY':None}


def _make_action_packet(observation,*,session,execute,next_target,collect=False,flight=False,axis=None,autorun=False,button=None):
    if axis is not None and axis not in AXIS_IDS:raise ReflectionError('Eixo fora do conjunto revisado.')
    if autorun:button='Move_AutoForward'
    if button is not None and button not in ('Move_AutoForward','Move_Ride','Move_Sprint','Move_FlyUp','Move_FlyDown','QuickSlot_Skill'):raise ReflectionError('Botão fora do conjunto revisado.')
    autorun=button is not None
    if sum((bool(next_target),bool(collect),bool(flight),bool(autorun),axis is not None))>1:raise ReflectionError('Ações incompatíveis.')
    name=axis if axis else button if autorun else 'Move_Fly' if flight else ('Action' if collect else ('Combat_NextTarget' if next_target else 'Macro'))
    single=next_target or collect or flight or (autorun and button not in ('Move_Sprint','Move_FlyUp','Move_FlyDown','QuickSlot_Skill'))
    if observation.get('action_name','Macro')!=name:
        raise ReflectionError('Um probe de Macro não aceita bindings de outra ação.')
    if type(execute) is not bool or not observation.get('player_present'):
        raise ReflectionError('É necessário um jogador atual e um modo de probe explícito.')
    scope=observation['frame_scope']
    if scope['session']!=session:raise ReflectionError('Bindings pertencem a outra sessão.')
    bindings=observation['bindings']
    if len(bindings)!=(1 if single else 2) or {item.get('event') for item in bindings}!=({1} if single else ({1,16} if axis else {2,16})):
        raise ReflectionError('O Macro precisa de um par único de início/término.')
    for item in bindings:
        if item.get('native_code_validated') is not True or (not autorun and not (axis and AXIS_IDS[axis] is None) and item['action_id']!=(AXIS_IDS[axis] if axis else 4 if flight else (66 if collect else (67 if next_target else 184)))) or item['delegate_event']!=item['event']:
            raise ReflectionError('Binding Macro não possui código, ID e trigger validados.')
    ordered=sorted(bindings,key=lambda value:value['event']);first,last=ordered[0],ordered[-1]
    if any(first[key]!=last[key] for key in ('method_rva','callback_rva','target_index','target_serial')):
        raise ReflectionError('Início/término do Macro apontam para destinos diferentes.')
    packet=MacroCyclePacket()
    roots=scope['roots']
    for destination, source in (('pool_rva','name_pool_rva'),('registry_rva','registry_rva'),('global_rva','global_weak_rva')):
        setattr(packet,destination,_unsigned(roots[source],64,source,minimum=1))
    roles=observation['frame_objects']
    for native,name in (('pawn','pawn'),('controller','controller'),('component','component'),('input','input_target'),('action','action')):
        ref=roles[name]
        if type(ref['index']) is not int or not 0<=ref['index']<2000000:
            raise ReflectionError('Identidade de objeto fora do limite.')
        address=_unsigned(ref['address'],64,'Endereço de objeto',minimum=1)
        serial=_unsigned(ref['serial'],32,'Serial de objeto')
        setattr(packet,native,NativeObjectRef(address,ref['index'],serial))
    if (packet.input.index,packet.input.serial)!=(first['target_index'],first['target_serial']):
        raise ReflectionError('O alvo do Macro não corresponde ao GamePlayerInput atual.')
    for prefix,item in (('start',first),('stop',last)):
        for suffix in ('binding','shared','delegate'):
            setattr(packet,prefix+'_'+suffix,_unsigned(item[suffix+'_address'],64,suffix,minimum=1))
    for field in ('method_rva','callback_rva'):
        setattr(packet,field,_unsigned(first[field],64,field,minimum=1))
    for field in ('pawn_controller_offset','controller_input_offset','controller_component_offset'):
        setattr(packet,field,_unsigned(scope[field],32,field,minimum=1))
    capture_size=roles['input_target']['class_size']
    if type(capture_size) is not int or not 40<=capture_size<=16384:raise ReflectionError('Layout de GamePlayerInput fora do limite.')
    packet.capture_size=capture_size
    packet.mode={'Move_AutoForward':12,'Move_Ride':14,'Move_Sprint':15,'Move_FlyUp':17,'Move_FlyDown':18,'QuickSlot_Skill':19}[button] if autorun else ({'Move_Forward':10,'Move_Right':11,'Camera_RotateX':13,'Camera_RotateY':16}[axis]) if axis else 9 if flight else (8 if collect else (2 if next_target else 0)+int(execute))
    if button=='QuickSlot_Skill':
        number=observation.get('action_number',0)
        if type(number) is not int or not 2<=number<=9:raise ReflectionError('Slot de skill fora de 1..8.')
        packet.macro_before=number
    return packet
