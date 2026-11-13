"""Read exact HP/MP after native layout and actor identity validation."""
import struct
from .name_pool import pointer
from .native_player import observe_player,ObjectRegistry,PlayerObservationError
from .native_scene import observe_scene
from .resource_layout import validate_resource_code,ResourceLayoutError
from .resource_container import capture_resources,ResourceContainerError
from .resource_values import decode_health,decode_mana,ResourceValueError


def actor_resources(registry,actor,layout):
    owners=registry.ancestry(actor)
    character=next((item for item in owners if item['name']=='AionCharacter'),None)
    offset=layout['private_offset']
    if not layout.get('read_layout_validated') or character is None or offset+8>character['size']:
        raise ResourceLayoutError('Layout de recursos não pertence ao AionCharacter validado.')
    process=registry.process;address=actor['address']+offset;raw=process.read(address,8)
    private=struct.unpack('<Q',raw)[0]
    if not private:return {'resources_observed':False,'stage':'character_private_absent'}
    pointer(private)
    if private%8:raise ResourceContainerError('Estrutura privada desalinhada.')
    values=capture_resources(process.read,private+layout['container_offset'],max_slots=512,max_bytes=65536)
    result={'resources_observed':False,'resource_key_count':len(values)}
    for name,decode in (('health',decode_health),('mana',decode_mana)):
        try:result[name]=decode(values).to_dict()
        except ResourceValueError as exc:result[name+'_unavailable_reason']=str(exc)
    if process.read(address,8)!=raw:raise ResourceContainerError('Estrutura privada mudou durante a leitura.')
    current=registry.resolve_pair(actor['index'],actor['serial'])
    if current is None or current['address']!=actor['address']:
        raise PlayerObservationError('Ator mudou durante a leitura de recursos.')
    result['resources_observed']='health' in result
    result['stage']='resource_values_observed' if result['resources_observed'] else 'health_values_unavailable'
    return result


def observe_resources(process,roots,layout,definitions):
    validated=validate_resource_code(lambda rva,size:process.read(process.base+rva,size),process.image_size,
                                     layout['source_rva'],definitions)
    if validated!= {key:layout[key] for key in validated}:
        raise ResourceLayoutError('Layout de recursos mudou.')
    player=observe_player(process,roots)
    result={'player_present':player['player_present'],'objects':[],'game_actions_executed':False,
            'combat_eligibility_validated':False,'gameplay_profile_promoted':False}
    if not player['player_present']:result['stage']=player['stage'];return result
    registry=ObjectRegistry(process,roots)
    role=player['roles']['player'];actor=registry.resolve_pair(role['index'],role['serial'])
    if actor is None:raise PlayerObservationError('Jogador mudou antes da leitura de recursos.')
    result['player']=actor_resources(registry,actor,validated)
    scene=observe_scene(process,roots);seen={(actor['index'],actor['serial'])}
    for collection in scene['collections']:
        for entry in collection['entries']:
            identity=(entry['index'],entry['serial'])
            if identity in seen or entry['class_name']!='BPCh_NpcBase_C':continue
            seen.add(identity)
            if len(result['objects'])>=8:continue
            observed={key:entry[key] for key in ('key_tag','key_payload_decimal','key_extra','index','serial','class_name')}
            try:
                npc=registry.resolve_pair(*identity)
                if npc is None:raise PlayerObservationError('NPC deixou de existir.')
                observed.update(actor_resources(registry,npc,validated))
            except (PlayerObservationError,ResourceContainerError,ResourceValueError,ResourceLayoutError) as exc:
                observed.update(resources_observed=False,stage='npc_resources_unavailable',reason=str(exc))
            result['objects'].append(observed)
    registry.verify();last=observe_player(process,roots)
    if not last['player_present'] or any(last['roles']['player'][key]!=role[key] for key in ('address','index','serial')):
        raise PlayerObservationError('Jogador mudou durante a consulta de recursos.')
    result['stage']='resource_observation_finished'
    return result
