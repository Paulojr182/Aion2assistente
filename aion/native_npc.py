"""Observe cached NPC definitions without assigning dynamic hostility."""
import struct
from .native_player import ObjectRegistry,PlayerObservationError
from .native_scene import observe_scene
from .native_npc_schema import observe_npc_schema
from .reflection_fields import ReflectionSchema,ReflectionError
from .npc_layout import validate_npc_layout


def decode_npc_row(raw,expected_id,enums):
    if type(raw) is not bytes or len(raw)!=236:raise ReflectionError('Registro de NPC incompleto.')
    npc_id=struct.unpack_from('<I',raw,8)[0]
    if not npc_id or npc_id!=expected_id:raise ReflectionError('ID do registro diverge da informação do NPC.')
    level=struct.unpack_from('<i',raw,56)[0]
    if not 0<=level<=1000:raise ReflectionError('Nível de NPC fora do limite.')
    booleans={}
    for name,offset in (('can_be_attacked',84),('mouse_over_able',201),('named',209)):
        value=raw[offset]
        if value not in (0,1):raise ReflectionError('Booleano inválido no registro de NPC.')
        booleans[name]=bool(value)
    result={'npc_definition_id':npc_id,'level':level,**booleans}
    for name,offset in (('NpcType',60),('NpcSubType',61),('NpcCreatureType',62),('RelationshipEntity',65),('NpcTargetAble',200),('FunctionType',210)):
        if name not in enums:raise ReflectionError('Declaração de enum de NPC ausente.')
        value=raw[offset];entries=[entry['name'] for entry in enums[name]['entries'] if int(entry['value_decimal'])==value]
        if len(entries)!=1:raise ReflectionError('Valor de enum de NPC ausente ou ambíguo: '+name)
        result[name]={'value':value,'name':entries[0]}
    result['base_definition_attackable']=bool(result['can_be_attacked']
        and result['NpcType']['name']=='ENpcType::Monster'
        and result['RelationshipEntity']['name']=='ERelationshipEntity::Monster'
        and result['NpcTargetAble']['name']=='ENpcTargetAble::Able')
    # This is a base-definition criterion, not a current actor relationship.
    result['hostility_assessed']=False
    return result


def read_cached_npc(registry,schema,actor,layout,enums):
    character=next((item for item in registry.ancestry(actor) if item['name']=='AionCharacter'),None)
    if character is None or max(layout['info']+8,layout['cache_control']+8)>character['size']:
        raise ReflectionError('Membros de NPC fora da classe proprietária.')
    info=struct.unpack('<Q',schema.read(actor['address']+layout['info'],8))[0]
    if not info:return {'definition_observed':False,'stage':'npc_info_absent'}
    npc_id=struct.unpack('<I',schema.read(info+4,4))[0]
    row,control=struct.unpack('<QQ',schema.read(actor['address']+layout['cache'],16))
    if not npc_id or not row or not control:return {'definition_observed':False,'stage':'npc_cached_definition_absent'}
    count=struct.unpack('<i',schema.read(control+8,4))[0]
    if not 0<count<=1000000:raise ReflectionError('Referência compartilhada do registro de NPC inválida.')
    raw=schema.read(row,236);vtable=struct.unpack_from('<Q',raw)[0];process=registry.process
    if not process.base<=vtable<=process.base+process.image_size-32:raise ReflectionError('Vtable de registro de NPC fora da imagem.')
    id_getter,name_getter=struct.unpack('<QQ',schema.read(vtable+16,16))
    for function,expected in ((id_getter,bytes.fromhex('8b41088902488bc2c3')),(name_getter,bytes.fromhex('488d410cc3'))):
        if not process.base<=function<=process.base+process.image_size-len(expected) or schema.read(function,len(expected))!=expected:
            raise ReflectionError('Getters de ID/nome do registro de NPC não conferem.')
    result=decode_npc_row(raw,npc_id,enums)
    for label,offset in (('internal_name',12),('resource_name',36)):
        name,number=schema.name(raw[offset:offset+8]);result[label]={'base':name,'number':number}
    current=registry.resolve_pair(actor['index'],actor['serial'])
    if current is None or current['address']!=actor['address']:raise PlayerObservationError('NPC foi substituído durante a leitura.')
    result.update(definition_observed=True,stage='cached_npc_definition_observed')
    return result


def observe_npcs(process,roots,layout,layout_definition,schema_definition):
    validated=validate_npc_layout(lambda rva,size:process.read(process.base+rva,size),process.image_size,layout['code_rva'],layout_definition)
    if validated!=layout:raise ReflectionError('Layout de NPC mudou.')
    declaration=observe_npc_schema(process,roots,schema_definition)
    if not declaration['npc_schema_validated']:raise ReflectionError('Declaração de NPC não validada.')
    scene=observe_scene(process,roots);registry=ObjectRegistry(process,roots);schema=ReflectionSchema(registry,max_bytes=262144)
    report={'objects':[],'game_actions_executed':False,'hostility_assessed':False};seen=set()
    for collection in scene['collections']:
        for entry in collection['entries']:
            identity=(entry['index'],entry['serial'])
            if identity in seen or entry['class_name']!='BPCh_NpcBase_C':continue
            seen.add(identity)
            if len(report['objects'])>=32:continue
            result={key:entry[key] for key in ('key_tag','key_payload_decimal','key_extra','index','serial')}
            try:
                actor=registry.resolve_pair(*identity)
                if actor is None:raise PlayerObservationError('NPC deixou de existir.')
                result.update(read_cached_npc(registry,schema,actor,layout,declaration['enums']))
            except (PlayerObservationError,ReflectionError) as exc:
                result.update(definition_observed=False,reason=str(exc))
            report['objects'].append(result)
    schema.verify();registry.verify();report['stage']='npc_cached_definitions_observed'
    return report
