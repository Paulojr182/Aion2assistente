"""Read reflected enhanced-input mappings; no keyboard events or game calls."""
import struct,json
from pathlib import Path
from .native_player import observe_player,ObjectRegistry,PlayerObservationError
from .reflection_fields import ReflectionSchema,ReflectionError
from .name_pool import NamePoolError
from .target_code import check_code


def struct_metadata(registry,schema,field):
    if field.kind!='StructProperty' or field.count!=1:raise ReflectionError('Campo não é struct simples.')
    address=struct.unpack('<Q',schema.read(field.address+112,8))[0]
    if registry.object(address)['class_name']!='ScriptStruct':raise ReflectionError('Struct não possui declaração ScriptStruct.')
    raw=schema.read(address,96);name,number=schema.name(raw[24:32])
    size,alignment=struct.unpack_from('<ii',raw,88)
    if number or size!=field.stride or not 1<=alignment<=16 or alignment&(alignment-1):
        raise ReflectionError('Extensão/alinhamento da struct de entrada não conferem.')
    fields=schema.own_fields(address,size,struct.unpack_from('<Q',raw,80)[0])
    return {'name':name,'size':size,'address':address,'fields':fields}


def observe_input_schema(process,roots):
    partial={'stage':'player_chain'}
    try:return _observe_input_schema(process,roots,partial)
    except (NamePoolError,ReflectionError,PlayerObservationError) as exc:
        raise ReflectionError(str(exc),details={'input_stage':partial['stage'],'partial_input_schema':partial,
            **(getattr(exc,'details',None) or {})}) from exc


def _observe_input_schema(process,roots,result):
    player=observe_player(process,roots)
    result.update(player_present=player['player_present'],game_actions_executed=False,macro_state_validated=False)
    if not player['player_present']:result['stage']=player['stage'];return result
    registry=ObjectRegistry(process,roots);schema=ReflectionSchema(registry,max_bytes=1048576)
    game=registry.object(player['roles']['game_instance']['address'])
    result['stage']='input_system_link';input_=schema.linked(game,'AionGameInstance','InputSystem')
    if input_ is None or input_['class_name']!='AionInputSystem':raise ReflectionError('Sistema de entrada ausente.')
    result['stage']='input_context_link';context=schema.linked(input_,'AionInputSystem','InputMappingContext')
    if context is None:
        schema.verify();registry.verify();result['stage']='input_mapping_context_absent';return result
    if context['class_name']!='InputMappingContext':raise ReflectionError('Classe do contexto de entrada mudou.')
    info=registry.class_info(context['class_address'])
    fields=schema.own_fields(context['class_address'],info['size'],struct.unpack_from('<Q',info['raw'],80)[0])
    result['context_fields']=[{'name':field.name,'kind':field.kind,'offset':field.offset,'stride':field.stride} for field in fields]
    mappings=[field for field in fields if field.name=='Mappings' and field.number==0]
    if len(mappings)!=1:raise ReflectionError('Array Mappings ausente ou ambíguo.')
    field=mappings[0]
    if (field.kind,field.count,field.stride)!=('ArrayProperty',1,16):raise ReflectionError('Representação de Mappings mudou.')
    schema.default_accessors(field)
    result['stage']='array_copy_code'
    template=json.loads((Path(__file__).resolve().parents[1]/'input_array_template.json').read_text(encoding='utf-8'))
    copy=struct.unpack('<Q',schema.read(field.vtable+240,8))[0]-process.base
    check_code(lambda rva,size:schema.read(process.base+rva,size),process.image_size,copy,template)
    result['stage']='array_inner_descriptor'
    extension=schema.read(field.address+112,16)
    if extension[0]:raise ReflectionError('Array de entrada não usa representação ordinária.')
    inner=struct.unpack_from('<Q',extension,8)[0];raw=schema.read(inner,72)
    type_pointer,owner=struct.unpack_from('<QQ',raw,8);count,stride=struct.unpack_from('<ii',raw,48)
    offset=struct.unpack_from('<i',raw,68)[0]
    if owner!=field.address or count!=1 or not 16<=stride<=1024 or offset or schema.type_info(type_pointer)!=('StructProperty',1048576,1081345):
        raise ReflectionError('Elemento do array de entrada não validado.')
    from types import SimpleNamespace
    inner_field=SimpleNamespace(kind='StructProperty',count=1,stride=stride,address=inner)
    result['stage']='mapping_struct_metadata';entry=struct_metadata(registry,schema,inner_field)
    if entry['name']!='EnhancedActionKeyMapping':raise ReflectionError('Array não contém EnhancedActionKeyMapping.')
    result['mapping_struct_fields']=[{'name':p.name,'kind':p.kind,'offset':p.offset,'stride':p.stride} for p in entry['fields']]
    keys=[p for p in entry['fields'] if p.name=='Key' and p.number==0]
    actions=[p for p in entry['fields'] if p.name=='Action' and p.number==0]
    if len(keys)!=1 or len(actions)!=1:raise ReflectionError('Key/Action ausentes ou ambíguos.')
    key=keys[0];action=actions[0]
    if (action.kind,action.count,action.stride)!=('ObjectProperty',1,8):raise ReflectionError('Representação da ação mudou.')
    schema.default_accessors(action);schema.default_accessors(key)
    result['stage']='key_struct_metadata';key_info=struct_metadata(registry,schema,key)
    if key_info['name']!='Key':raise ReflectionError('Chave de entrada não pertence a FKey.')
    result['key_struct_fields']=[{'name':p.name,'kind':p.kind,'offset':p.offset,'stride':p.stride} for p in key_info['fields']]
    key_names=[p for p in key_info['fields'] if p.name=='KeyName' and p.number==0]
    if len(key_names)!=1 or (key_names[0].kind,key_names[0].count,key_names[0].stride)!=('NameProperty',1,8):
        result['stage']='key_name_not_reflected';schema.verify();registry.verify();return result
    name_field=key_names[0];schema.default_accessors(name_field)
    result['stage']='mapping_values'
    storage,count,capacity=struct.unpack('<Qii',schema.read(context['address']+field.offset,16))
    if not 0<=count<=512 or not count<=capacity<=2048:raise ReflectionError('Array de entradas fora do limite.')
    result['mapping_count']=count;result['bindings']=[]
    entries=schema.read(storage,count*stride) if count else b''
    for index in range(count):
        raw=entries[index*stride:(index+1)*stride]
        key_name,key_number=schema.name(raw[key.offset+name_field.offset:key.offset+name_field.offset+8])
        address=struct.unpack_from('<Q',raw,action.offset)[0]
        report={'key':key_name,'key_number':key_number}
        if address:
            try:
                input_action=registry.object(address)
                if input_action['class_name']!='InputAction':raise ReflectionError('Ação não pertence a InputAction.')
                action_name,action_number=schema.name(schema.read(address+24,8))
                report.update(action=action_name,action_number=action_number)
            except (PlayerObservationError,ReflectionError) as exc:report['action_unavailable_reason']=str(exc)
        result['bindings'].append(report)
    schema.verify();registry.verify();result['stage']='input_bindings_observed'
    return result
