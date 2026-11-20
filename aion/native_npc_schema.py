"""Validate reflected NPC definition fields; does not classify hostile actors."""
import struct
from .native_player import ObjectRegistry,PlayerObservationError
from .name_pool import NamePoolError,NamePoolReader,pointer
from .reflection_fields import ReflectionSchema,ReflectionError


def validate_npc_fields(fields,definition):
    selected=[]
    for expected in definition['fields']:
        matches=[field for field in fields if field.name==expected['name'] and field.number==0]
        if len(matches)!=1:raise ReflectionError('Campo de NPC ausente ou ambíguo: '+expected['name'])
        field=matches[0]
        if any(getattr(field,key)!=expected[key] for key in ('offset','kind','number','count','stride','type_id','cast_flags')):
            raise ReflectionError('Formato de campo de NPC mudou: '+field.name)
        selected.append(field)
    return tuple(selected)


def find_npc_declarations(registry,definition,*,max_bytes=134217728,max_slots=1048576):
    process=registry.process;used=0;matches=[];scanned=0;names={}
    slots_found=[];find_npc_declarations.last_slots=slots_found
    if not 1<=max_bytes<=134217728 or not 1<=max_slots<=1048576:
        raise ReflectionError('Limites de descoberta de NPC inválidos.')
    def read(address,size):
        nonlocal used
        if type(size) is not int or not 0<size<=max_bytes-used:
            raise ReflectionError('Orçamento de descoberta de NPC esgotado.')
        used+=size;raw=process.read(address,size)
        if type(raw) is not bytes or len(raw)!=size:raise ReflectionError('Leitura de descoberta de NPC incompleta.')
        return raw
    if not 1<=len(definition['name'])<=64:raise ReflectionError('Nome procurado fora do limite.')
    extent=min(registry.count,max_slots)
    for first in range(0,extent,65536):
        chunk=pointer(struct.unpack('<Q',read(registry.chunks+8*(first>>16),8))[0])
        count=min(65536,extent-first);slots=read(chunk,count*24)
        for local in range(count):
            scanned+=1;offset=local*24
            address=struct.unpack_from('<Q',slots,offset)[0];flags=struct.unpack_from('<I',slots,offset+8)[0]
            if not address or flags&0x30200000:continue
            try:
                raw=read(pointer(address),40);name_index,number=struct.unpack_from('<II',raw,24)
                if number:continue
                if name_index not in names:
                    # Each distinct name has its own small budget; the complete
                    # search has an independent hard budget. Exhaustion cannot
                    # silently turn the rest of a search into "no candidates".
                    reader=NamePoolReader(read,registry.names.pool,budget=2048,max_length=64)
                    try:names[name_index]=reader.resolve(name_index)==definition['name']
                    except NamePoolError:names[name_index]=False
                if not names[name_index]:continue
                object_=registry.object(address)
                if object_['class_name']=='ScriptStruct':matches.append(object_);slots_found.append(first+local)
            except (PlayerObservationError,NamePoolError):continue
    return matches,scanned,used,registry.count>extent


def observe_npc_schema(process,roots,definition):
    registry=ObjectRegistry(process,roots);schema=ReflectionSchema(registry,max_bytes=262144,max_fields=128)
    matches=[];scanned=0;used=0;truncated=False;hint=definition.get('observed_index_hint')
    if type(hint) is int and 0<=hint<registry.count:
        try:
            address,_=registry.slot(hint);raw=process.read(address,40)
            name_index,number=struct.unpack_from('<II',raw,24)
            if not number and registry.names.resolve(name_index)==definition['name']:
                candidate=registry.object(address)
                if candidate['class_name']=='ScriptStruct':matches=[candidate]
        except (PlayerObservationError,NamePoolError):pass
    method='typed_observed_index_hint'
    if not matches:
        matches,scanned,used,truncated=find_npc_declarations(registry,definition);method='bounded_registry_search'
        found_slots=getattr(find_npc_declarations,'last_slots',[])
        if len(matches)==1 and len(found_slots)==1:
            # guarda a posicao achada como dica para as proximas conexoes (versao nova do jogo)
            try:
                import json
                from pathlib import Path
                path=Path(__file__).resolve().parent.parent/'npc_schema.json'
                data=json.loads(path.read_text('utf-8'));data['observed_index_hint']=found_slots[0]
                path.write_text(json.dumps(data,indent=1,ensure_ascii=False),'utf-8')
            except Exception:
                pass
    report={'game_actions_executed':False,'combat_eligibility_validated':False,'scanned_slots':scanned,
            'candidate_count':len(matches),'npc_schema_validated':False,'search_bytes':used,'search_truncated':truncated,
            'discovery_method':method}
    if len(matches)!=1:
        report['stage']='npc_declaration_unavailable_or_ambiguous';registry.verify();return report
    object_=matches[0];raw=schema.read(object_['address'],96)
    size,alignment=struct.unpack_from('<ii',raw,88)
    if (size,alignment)!=(definition['size'],definition['alignment']):
        raise ReflectionError('Extensão ou alinhamento da declaração de NPC mudou.',details={'size':size,'alignment':alignment})
    fields=schema.own_fields(object_['address'],size,struct.unpack_from('<Q',raw,80)[0])
    selected=validate_npc_fields(fields,definition)
    enums={}
    for field in selected:
        schema.default_accessors(field)
        if field.kind=='EnumProperty':enums[field.name]=schema.enum_values(field)
    schema.verify();registry.verify()
    report.update(npc_schema_validated=True,stage='npc_definition_fields_observed',
                  declaration_index=object_['index'],field_count=len(selected),size=size,enums=enums,
                  fields=[{'name':field.name,'offset':field.offset,'kind':field.kind} for field in selected])
    return report
