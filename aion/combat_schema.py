"""Bounded reflected combat/input metadata inventory, no game actions."""
import re,struct
from .native_player import observe_player,ObjectRegistry,PlayerObservationError
from .reflection_fields import ReflectionSchema,ReflectionError

FILTER=re.compile('macro|auto|input|skill|target|combat|attack|quick|action',re.I)


def method_inventory(registry,schema,owner,info):
    following=struct.unpack_from('<Q',info['raw'],72)[0];seen=set();result=[]
    while following:
        if following in seen or len(seen)>=512:raise ReflectionError('Lista de métodos de combate inválida.')
        seen.add(following);function=registry.object(following)
        if function['class_name']!='Function' or function['class_size']!=224:
            raise ReflectionError('Metadados de função não possuem o layout revisado.')
        header=schema.read(following,224);name,number=schema.name(header[24:32])
        if number or struct.unpack_from('<Q',header,32)[0]!=owner:
            raise ReflectionError('Método não pertence à classe de combate selecionada.')
        native=struct.unpack_from('<Q',header,216)[0]
        if native and not registry.process.base<=native<registry.process.base+registry.process.image_size:
            raise ReflectionError('Método nativo de combate fora da imagem.')
        result.append({'name':name,'native_rva':native-registry.process.base if native else None})
        following=struct.unpack_from('<Q',header,40)[0]
    return result


def observe_combat_schema(process,roots):
    player=observe_player(process,roots)
    result={'player_present':player['player_present'],'roles':{},'game_actions_executed':False,
            'macro_state_validated':False,'skills_validated':False}
    if not player['player_present']:result['stage']=player['stage'];return result
    follow=[]
    for role,owner in (('game_instance','AionGameInstance'),('session','AionCharSession'),('player','AionCharacter')):
        registry=ObjectRegistry(process,roots);schema=ReflectionSchema(registry,max_bytes=524288)
        actor=registry.object(player['roles'][role]['address'])
        # field() traverses owners and verifies property ownership; get the
        # owner descriptor first so every reported field comes from that class.
        address=actor['class_address'];seen=set();info=None
        while address:
            if address in seen or len(seen)>=32:raise ReflectionError('Hierarquia de inventário de combate inválida.')
            seen.add(address);candidate=registry.class_info(address)
            if candidate['name']==owner:info=candidate;break
            address=candidate['parent']
        if info is None:raise ReflectionError('Proprietário do inventário de combate ausente.')
        fields=schema.own_fields(address,info['size'],struct.unpack_from('<Q',info['raw'],80)[0]);selected=[]
        for field in fields:
            if not FILTER.search(field.name):continue
            report={'name':field.name,'kind':field.kind,'offset':field.offset,'count':field.count,'stride':field.stride}
            if field.kind=='ObjectProperty' and field.count==1 and field.stride==8:
                try:
                    linked=schema.linked(actor,owner,field.name)
                    if linked:
                        report['linked_class']=linked['class_name'];follow.append((role+'.'+field.name,linked))
                    else:report['linked_absent']=True
                except (PlayerObservationError,ReflectionError) as exc:report['link_unavailable_reason']=str(exc)
            selected.append(report)
        schema.verify();registry.verify();result['roles'][role]={'owner':owner,'total_fields':len(fields),'matches':selected}
    result['linked_owners']={}
    for label,linked in follow[:12]:
        registry=ObjectRegistry(process,roots);schema=ReflectionSchema(registry,max_bytes=262144)
        actor=registry.resolve_pair(linked['index'],linked['serial']) if linked['serial'] else registry.object(linked['address'])
        if actor is None or actor['address']!=linked['address']:raise PlayerObservationError('Sistema de combate mudou.')
        info=registry.class_info(actor['class_address'])
        fields=schema.own_fields(actor['class_address'],info['size'],struct.unpack_from('<Q',info['raw'],80)[0])
        result['linked_owners'][label]={'class_name':actor['class_name'],'total_fields':len(fields),
            'fields':[{'name':field.name,'kind':field.kind,'offset':field.offset,'count':field.count,'stride':field.stride}
                      for field in fields[:128]],'report_truncated':len(fields)>128}
        methods=method_inventory(registry,schema,actor['class_address'],info)
        result['linked_owners'][label].update(method_count=len(methods),methods=methods[:96],methods_truncated=len(methods)>96)
        schema.verify();registry.verify()
    result['linked_owners_truncated']=len(follow)>12;result['stage']='combat_metadata_inventory_observed'
    return result
