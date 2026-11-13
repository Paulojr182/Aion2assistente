"""Offline straight-line approach geometry; no terrain or flight authorization."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Approach3D:
    distance:float
    interaction_range:float
    stop_distance:float
    direction:tuple[float,float,float]
    waypoints:tuple[tuple[float,float,float],...]
    requires_vertical_control:bool
    obstacle_clearance_verified:bool=False
    executable:bool=False


def requires_flight(profession,start,target,interaction_range):
    """Single rule deciding flight vs. walking for a gather approach.

    Walk whenever the height difference fits the interaction sphere or looks
    like ordinary terrain (slope). Fly for Od sources out of vertical reach and
    for any source clearly above/below the character (cliff, floating spot).
    """
    dz=abs(target[2]-start[2])
    if dz<=interaction_range:return False
    if profession=='Od':return True
    planar=math.hypot(target[0]-start[0],target[1]-start[1])
    return planar<1. or dz>350. or dz>planar


def approach_3d(start,target,interaction_range,*,segment_length=200.,margin=.2):
    def vector(value):
        if (not isinstance(value,(tuple,list)) or len(value)!=3
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in value)):
            raise ValueError('Posição 3D inválida.')
        return tuple(float(v) for v in value)
    start,target=vector(start),vector(target)
    if (type(interaction_range) not in (int,float) or not math.isfinite(interaction_range)
            or not 0<interaction_range<=10000
            or type(segment_length) not in (int,float) or not math.isfinite(segment_length)
            or not 1<=segment_length<=1000
            or type(margin) not in (int,float) or not math.isfinite(margin) or not 0<=margin<1):
        raise ValueError('Limites de aproximação inválidos.')
    delta=tuple(b-a for a,b in zip(start,target));distance=math.hypot(*delta)
    if not math.isfinite(distance) or distance>100000:
        raise ValueError('Destino excede o limite de planejamento local.')
    stop_distance=interaction_range*(1-margin)
    # Already in the actual interaction sphere: no additional travel needed.
    travel=max(0.,distance-stop_distance) if distance>interaction_range else 0.
    count=math.ceil(travel/segment_length)
    if count>1024:raise ValueError('Quantidade de segmentos excedida.')
    direction=tuple(v/distance for v in delta) if travel else (0.,0.,0.)
    points=tuple(tuple(a+d*min(i*segment_length,travel) for a,d in zip(start,direction))
                 for i in range(1,count+1))
    return Approach3D(distance,float(interaction_range),stop_distance,direction,points,
                      abs(delta[2])>interaction_range)


def plan_gather_approach(report,*,target_identity=None):
    """Use a saved, same-world source report as geometry, never live evidence."""
    sources=report.get('gather_sources',report)
    if sources.get('player_present') is not True or not sources.get('session'):
        raise ValueError('Relatório sem personagem/sessão.')
    origin=sources.get('player_position',{})
    def position(sample):
        if (sample.get('position_observed') is not True or sample.get('units')!='engine_units'
                or not isinstance(sample.get('world'),dict)):
            raise ValueError('Posição ou mundo não verificados no relatório.')
        world=sample['world']
        key=tuple(world.get(k) for k in ('address','index','serial'))
        if any(type(v) is not int or v<0 for v in key) or not key[0]:
            raise ValueError('Identidade do mundo inválida.')
        return sample.get('position'),key
    start,world=position(origin)
    candidates=[];identities=set()
    for target in sources.get('targets',()):
        identity=target.get('identity')
        if (not isinstance(identity,(tuple,list)) or len(identity)!=2
                or any(type(v) is not int or v<0 for v in identity)):
            raise ValueError('Identidade de recurso inválida.')
        identity=tuple(identity)
        if identity in identities:raise ValueError('Recurso duplicado no relatório.')
        identities.add(identity)
        if target_identity is not None and identity!=tuple(target_identity):continue
        if target.get('mastery_requirement_met') is not True:continue
        destination,target_world=position(target.get('position',{}))
        if world!=target_world:raise ValueError('Recurso pertence a outro mundo.')
        dest_point = destination
        profession=target.get('profession') or (target.get('source') or {}).get('profession')
        if not requires_flight(profession,start,destination,target.get('interaction_range_engine_units') or 130.):
            dest_point = (destination[0], destination[1], start[2])
        route=approach_3d(start,dest_point,target.get('interaction_range_engine_units'))
        candidates.append((route.distance,identity,route,target))
    if not candidates:raise ValueError('Nenhum recurso elegível no relatório.')
    _,identity,route,target=min(candidates,key=lambda row:(row[0],row[1]))
    return identity,route
