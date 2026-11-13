"""Discover and validate current native Macro bindings; never invoke them."""
import struct
from .native_player import observe_player,ObjectRegistry,PlayerObservationError
from .reflection_fields import ReflectionSchema,ReflectionError
from .target_code import check_code

FLIGHT_ACTION_NAMES=('Move_Fly','Move_FlyUp','Move_FlyDown')
AUTORUN_ACTION_NAMES=('Move_AutoForward','Move_Ride','Move_Sprint','QuickSlot_Skill')
AXIS_DIAGNOSTIC_NAMES=('Move_Forward','Move_Right','Camera_RotateX','Camera_RotateY')


def binding_header(raw):
    if type(raw) is not bytes or len(raw)!=25:raise ReflectionError('Cabeçalho de binding incompleto.')
    index,serial=struct.unpack_from('<iI',raw,16)
    return index,serial,raw[24]


def delegate_header(raw):
    if type(raw) is not bytes or len(raw)!=40:raise ReflectionError('Delegate de ação incompleto.')
    vtable=struct.unpack_from('<Q',raw)[0];action,event=raw[8:10]
    index,serial=struct.unpack_from('<iI',raw,24);method=struct.unpack_from('<Q',raw,32)[0]
    if event not in (1,2,4,8,16) or index<0 or not serial:raise ReflectionError('Delegate não possui trigger e identidade fraca válidos.')
    return vtable,action,event,index,serial,method


def delegate_allocation(read,binding_address):
    raw=read(binding_address+32,8)
    if type(raw) is not bytes or len(raw)!=8:raise ReflectionError('Ponteiro de contêiner de delegate incompleto.')
    shared=struct.unpack('<Q',raw)[0];raw=read(shared,12)
    if type(raw) is not bytes or len(raw)!=12:raise ReflectionError('Contêiner de delegate incompleto.')
    allocation,allocated=struct.unpack('<Qi',raw)
    return shared,allocation,allocated


def observe_action_bindings(process,roots,definitions,*,action_name='Macro',diagnostic_only=False,action_number=0):
    if type(diagnostic_only) is not bool:raise ReflectionError('Modo de diagnóstico inválido.')
    if action_name not in ('Macro','Combat_NextTarget','Action')+FLIGHT_ACTION_NAMES+AUTORUN_ACTION_NAMES+AXIS_DIAGNOSTIC_NAMES:
        raise ReflectionError('Ação fora do conjunto de observação revisado.')
    player=observe_player(process,roots)
    result={'player_present':player['player_present'],'action_name':action_name,'action_number':action_number,'bindings':[],'game_actions_executed':False,
            'background_dispatch_validated':False,'macro_state_validated':False}
    if not player['player_present']:result['stage']=player['stage'];return result
    registry=ObjectRegistry(process,roots);schema=ReflectionSchema(registry,max_bytes=1048576)
    pawn=registry.object(player['roles']['player']['address'])
    controller=schema.linked(pawn,'Pawn','Controller')
    if controller is None:result['stage']='controller_absent';schema.verify();registry.verify();return result
    if controller['class_name']!='AionPlayerController':raise ReflectionError('Controlador do jogador não validado.')
    target=schema.linked(controller,'AionPlayerController','GamePlayerInput')
    component=schema.linked(controller,'Actor','InputComponent')
    if target is None or component is None:result['stage']='internal_input_objects_absent';schema.verify();registry.verify();return result
    if target['class_name']!='AionGamePlayerInput' or component['class_name']!='AionInputComponent' or component['class_size']!=384:
        raise ReflectionError('Classes ou layout da entrada interna mudaram.',details={'target_class':target['class_name'],
            'component_class':component['class_name'],'component_size':component['class_size']})
    for object_ in (target,component):
        if struct.unpack('<Q',schema.read(object_['address']+32,8))[0]!=controller['address']:
            raise ReflectionError('Entrada interna pertence a outro controlador.')
    storage,count,capacity=struct.unpack('<Qii',schema.read(component['address']+328,16))
    if not 0<=count<=capacity<=1024 or storage%8:raise ReflectionError('Array de bindings fora dos limites.')
    def reference(object_):
        return {key:object_[key] for key in ('address','index','serial','class_address','class_name','class_size')}
    result['frame_objects']={'pawn':reference(pawn),'controller':reference(controller),
        'component':reference(component),'input_target':reference(target)}
    result['frame_scope']={'session':process.session,'module_base':process.base,'image_size':process.image_size,
        'roots':dict(roots),'pawn_controller_offset':schema.field(pawn,'Pawn','Controller').offset,
        'controller_input_offset':schema.field(controller,'AionPlayerController','GamePlayerInput').offset,
        'controller_component_offset':schema.field(controller,'Actor','InputComponent').offset}
    pointers=schema.read(storage,count*8) if count else b''
    records={item['name']:item for item in definitions};expired=0;codes={}
    for offset in range(0,len(pointers),8):
        address=struct.unpack_from('<Q',pointers,offset)[0]
        raw=schema.read(address,25);index,serial,event=binding_header(raw)
        try:action=registry.resolve_pair(index,serial)
        except PlayerObservationError:expired+=1;continue
        if action is None:expired+=1;continue
        name,number=schema.name(schema.read(action['address']+24,8))
        if (name,number)!=(action_name,action_number):continue
        if action['class_name']!='InputAction':raise ReflectionError('Binding Macro não pertence a InputAction.')
        if 'action' in result['frame_objects'] and result['frame_objects']['action']!=reference(action):
            raise ReflectionError('Os bindings Macro usam assets diferentes.')
        result['frame_objects']['action']=reference(action)
        shared,allocation,allocated=delegate_allocation(schema.read,address)
        if allocated!=3 or allocation%8:
            diagnostic={'binding_address':address,'event':event,'allocated_units':allocated,
                        'native_code_validated':False,'stage':'unreviewed_macro_delegate_layout'}
            if 0<allocated<=8 and allocation and allocation%8==0:
                raw=schema.read(allocation,min(allocated*16,128));diagnostic['diagnostic_delegate_hex']=raw.hex()
                if len(raw)>=8:
                    vtable=struct.unpack_from('<Q',raw)[0]
                    if process.base<=vtable<=process.base+process.image_size-96:
                        callback=struct.unpack('<Q',schema.read(vtable+88,8))[0]
                        if process.base<=callback<=process.base+process.image_size-256:
                            diagnostic['diagnostic_callback_rva']=callback-process.base
                            diagnostic['diagnostic_callback_prefix_hex']=schema.read(callback,256).hex()
            result['bindings'].append(diagnostic)
            continue
        raw=schema.read(allocation,40);vtable,action_id,delegate_event,target_index,target_serial,method=delegate_header(raw)
        if delegate_event!=event:raise ReflectionError('Trigger do delegate não coincide com o binding Macro.')
        resolved=registry.resolve_pair(target_index,target_serial)
        if resolved is None or resolved['address']!=target['address']:raise ReflectionError('Delegate Macro aponta para outro GamePlayerInput.')
        if not process.base<=vtable<=process.base+process.image_size-96:raise ReflectionError('Vtable de delegate fora da imagem.')
        callback=struct.unpack('<Q',schema.read(vtable+88,8))[0]
        if diagnostic_only:
            if not process.base<=method<=process.base+process.image_size-256 or not process.base<=callback<=process.base+process.image_size-256:
                raise ReflectionError('Código de diagnóstico fora da imagem.')
            result['bindings'].append({'binding_address':address,'shared_address':shared,'delegate_address':allocation,
                'event':event,'action_id':action_id,'delegate_event':delegate_event,
                'method_rva':method-process.base,'callback_rva':callback-process.base,
                'method_bytes':schema.read(method,256).hex(),'callback_bytes':schema.read(callback,256).hex(),
                'target_index':target_index,'target_serial':target_serial,
                'native_code_validated':False,'call_signature_reviewed':False})
            continue
        axis=action_name in AXIS_DIAGNOSTIC_NAMES
        for name,function in ((('axis_method' if axis else 'boolean_method'),method),(('axis_delegate' if axis else 'boolean_delegate'),callback)):
            rva=function-process.base
            if (name,rva) not in codes:
                code=check_code(lambda location,size:schema.read(process.base+location,size),process.image_size,rva,records[name])
                if name.endswith('_delegate'):
                    references=[rva+end+struct.unpack_from('<i',code,operand)[0] for operand,end in records[name]['registry_references']]
                    if references!=[roots['registry_rva']+36,roots['registry_rva']+16]:
                        raise ReflectionError('Registro usado pelo delegate Macro não coincide com as raízes atuais.')
                codes[(name,rva)]=True
        result['bindings'].append({'binding_address':address,'shared_address':shared,'delegate_address':allocation,'event':event,
            'action_id':action_id,'delegate_event':delegate_event,'method_rva':method-process.base,
            'callback_rva':callback-process.base,'target_index':target_index,'target_serial':target_serial,
            'native_code_validated':True})
    schema.verify();registry.verify()
    last=observe_player(process,roots)
    if not last['player_present'] or any(last['roles']['player'][key]!=player['roles']['player'][key] for key in ('address','index','serial')):
        raise PlayerObservationError('Jogador mudou durante a leitura de bindings internos.')
    result.update(stage='internal_macro_bindings_observed',binding_count=count,expired_or_unresolved=expired,
                  action_binding_count=len(result['bindings']),
                  macro_binding_count=len(result['bindings']) if action_name=='Macro' else 0)
    return result
