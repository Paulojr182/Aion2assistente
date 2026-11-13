"""Reflected and cross-checked actor coordinates. No navigation or game input."""
import math,struct,time
from .native_player import ObjectRegistry,observe_player,PlayerObservationError
from .native_scene import observe_scene
from .reflection_fields import ReflectionSchema,ReflectionError
from .name_pool import NamePoolError


class PositionMismatch(ReflectionError):
    pass


def position_pair(relative,cached):
    if type(relative) is not bytes or type(cached) is not bytes or len(relative)!=24 or len(cached)!=24:
        raise ReflectionError('Coordenadas devem conter três doubles imutáveis.')
    first=struct.unpack('<3d',relative);second=struct.unpack('<3d',cached)
    if not all(math.isfinite(value) for value in first+second):
        raise ReflectionError('Coordenada não finita.')
    if not all(math.isclose(a,b,rel_tol=1e-9,abs_tol=0.01) for a,b in zip(first,second)):
        raise PositionMismatch('Posição relativa e transform de mundo não concordam.')
    return second


def read_position_pair(read, relative_address, cached_address, attempts=3):
    """Reamostra leitura concorrente; cada tentativa ainda exige dois valores concordantes."""
    for attempt in range(attempts):
        relative = read(relative_address, 24)
        cached = read(cached_address, 24)
        try:
            return position_pair(relative, cached)
        except PositionMismatch:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.002)
    raise ReflectionError('Nenhuma amostra de posi??o dispon?vel.')


def actor_position(registry,schema,actor):
    if 'Actor' not in {info['name'] for info in registry.ancestry(actor)}:
        raise ReflectionError('O objeto não possui ancestral Actor.')
    process=registry.process
    outer=struct.unpack('<Q',schema.read(actor['address']+32,8))[0]
    if not outer:return {'position_observed':False,'stage':'actor_level_absent'}
    try:level=registry.object(outer)
    except PlayerObservationError as exc:
        raise PlayerObservationError(str(exc),details={**exc.details,'position_stage':'actor_level'}) from exc
    if level['class_name']!='Level':raise ReflectionError('O ator não pertence a um Level validado.')
    world=schema.linked(level,'Level','OwningWorld')
    if world is None or world['class_name']!='World':raise ReflectionError('Mundo do Level não validado.')
    root=schema.linked(actor,'Actor','RootComponent',448)
    if root is None:return {'position_observed':False,'stage':'root_component_absent'}
    owners=registry.ancestry(root)
    scene=next((info for info in owners if info['name']=='SceneComponent'),None)
    if scene is None or scene['size']!=720:raise ReflectionError('Layout de SceneComponent não validado.')
    if struct.unpack('<Q',schema.read(root['address']+32,8))[0]!=actor['address']:
        raise ReflectionError('RootComponent pertence a outro ator.')
    parent=schema.linked(root,'SceneComponent','AttachParent',208)
    if parent is not None:
        return {'position_observed':False,'stage':'attached_root_requires_transform_composition'}
    field=schema.vector_metadata(root)
    started=time.monotonic_ns()
    point=read_position_pair(process.read,root['address']+field.offset,root['address']+656)
    return {'position_observed':True,'stage':'unattached_world_position_observed',
            'position':point,'units':'engine_units','started_monotonic_ns':started,
            'finished_monotonic_ns':time.monotonic_ns(),
            'world':{key:world[key] for key in ('address','index','serial')},
            'root_component_class':root['class_name']}


def observe_positions(process,roots):
    scene=observe_scene(process,roots)
    player=observe_player(process,roots)
    if not player['player_present']:
        return {'player_present':False,'stage':player['stage'],'game_actions_executed':False}
    registry=ObjectRegistry(process,roots);schema=ReflectionSchema(registry)
    player_object=registry.resolve_pair(player['roles']['player']['index'],player['roles']['player']['serial'])
    if player_object is None:raise PlayerObservationError('O jogador mudou antes da leitura de posição.')
    player_position=actor_position(registry,schema,player_object)
    report={'player_present':True,'player_position':player_position,'objects':[],
            'game_actions_executed':False,'combat_eligibility_validated':False}
    entries=[];seen=set()
    for collection in scene['collections']:
        for entry in collection['entries']:
            key=(entry['index'],entry['serial'])
            if key not in seen and 'Actor' in entry['ancestry']:
                seen.add(key);entries.append(entry)
    entries.sort(key=lambda value:(value['class_name']!='BPCh_NpcBase_C',value['index']))
    for entry in entries[:16]:
        result={key:entry[key] for key in ('key_tag','key_payload_decimal','key_extra','class_name','index','serial')}
        try:
            object_=registry.resolve_pair(entry['index'],entry['serial'])
            if object_ is None:raise PlayerObservationError('Referência da entidade expirou.')
            observed=actor_position(registry,schema,object_)
            if observed['position_observed'] and player_position['position_observed']:
                if observed['world']!=player_position['world']:
                    observed={'position_observed':False,'stage':'different_world'}
                else:
                    observed['distance_engine_units']=math.dist(observed['position'],player_position['position'])
            result.update(observed)
        except (OSError,ReflectionError,PlayerObservationError,NamePoolError) as exc:
            result.update({'position_observed':False,'error':str(exc)})
        report['objects'].append(result)
    schema.verify();registry.verify()
    report['stage']='reflected_positions_observed'
    report['report_truncated']=len(entries)>16
    return report
