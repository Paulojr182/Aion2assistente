"""Observe the original manager's two reachable object collections, no input."""
from .native_player import observe_player,ObjectRegistry,PlayerObservationError
from .scene_objects import read_collection,SceneReadError


def observe_scene(process,roots,*,report_limit=64,known_keys_only=False,class_filter=None):
    if type(report_limit) is not int or not 0<=report_limit<=1024:
        raise SceneReadError('Limite do relatório inválido.')
    player=observe_player(process,roots)
    report={'player_present':player['player_present'],'collections':[],
            'game_actions_executed':False,'combat_eligibility_validated':False}
    if not player['player_present']:
        report['stage']=player['stage'];return report
    registry=ObjectRegistry(process,roots)
    manager=player['roles']['object_manager']
    current=registry.object(manager['address'])
    if (current['index'],current['serial'],current['class_name'])!=(manager['index'],manager['serial'],'AionObjectManager'):
        raise SceneReadError('O gerenciador de objetos mudou.')
    collections=[];ancestries={}
    for offset in (400,720):
        if offset+76>manager['class_size']:raise SceneReadError('Coleção fora da classe proprietária.')
        collection=read_collection(process.read,manager['address']+offset,max_nodes=1024,known_keys_only=known_keys_only)
        collections.append(collection)
        entries=[];expired=0;validated=0
        for node in collection.nodes:
            try:
                object_=registry.resolve_pair(node.weak_index,node.weak_serial)
                if object_ is None:expired+=1;continue
                if class_filter and object_.get('class_name')!=class_filter:continue
                klass=object_['class_address']
                if klass not in ancestries:
                    ancestries[klass]=[value['name'] for value in registry.ancestry(object_)]
                validated+=1
                if len(entries)<report_limit:
                    entries.append({'key_tag':node.key.tag,'key_payload_decimal':str(node.key.payload),
                        'key_extra':node.key.extra,**object_,'ancestry':ancestries[klass],
                        'combat_eligibility_validated':False})
            except PlayerObservationError:
                expired+=1
        report['collections'].append({'offset':offset,'reachable_nodes':len(collection.nodes),
            'validated_objects':validated,'expired_or_unresolved':expired,'entries':entries,
            'reported_objects':len(entries),'report_truncated':validated>len(entries)})
    for collection in collections:collection.verify(process.read)
    registry.verify()
    current_player=observe_player(process,roots)
    if not current_player['player_present']:
        raise SceneReadError('O jogador deixou de estar presente.')
    for role in ('game_instance','session','object_manager','player'):
        first=player['roles'][role];last=current_player['roles'][role]
        if (first['address'],first['index'],first['serial'])!=(last['address'],last['index'],last['serial']):
            raise SceneReadError('A cadeia do jogador mudou durante a leitura de entidades.')
    report['stage']='reachable_scene_objects_observed'
    return report
