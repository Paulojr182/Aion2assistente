"""Controlled read-only macro-state comparison; differences are not semantics."""
from dataclasses import dataclass
import hashlib,time
from .native_player import observe_player,ObjectRegistry,PlayerObservationError
from .native_target import hud_roots
from .reflection_fields import ReflectionSchema,ReflectionError


@dataclass(frozen=True)
class MacroSnapshot:
    session:str
    player:tuple
    observed_ns:int
    roles:dict


def snapshot_summary(snapshot):
    return {'session':snapshot.session,'observed_monotonic_ns':snapshot.observed_ns,
            'roles':{name:{'class_name':item['class_name'],'size':len(item['data']),
                         'sha256':hashlib.sha256(item['data']).hexdigest()} for name,item in snapshot.roles.items()},
            'macro_state_validated':False,'game_actions_executed':False}


def compare_macro_snapshots(first,last):
    if first.session!=last.session or first.player!=last.player or first.roles.keys()!=last.roles.keys():
        raise ReflectionError('Sessão, jogador ou conjunto de sistemas mudou durante a comparação do macro.')
    report={'roles':{},'macro_state_validated':False,'game_actions_executed':False}
    for name,before in first.roles.items():
        after=last.roles[name]
        if before['identity']!=after['identity'] or before['class_name']!=after['class_name'] or len(before['data'])!=len(after['data']):
            raise ReflectionError('Identidade ou layout do sistema de macro mudou.')
        old,new=before['data'],after['data']
        changes=[index for index,(a,b) in enumerate(zip(old,new)) if a!=b]
        report['roles'][name]={'changed_bytes':len(changes),'report_truncated':len(changes)>64,
            'changes':[{'offset':index,'before':old[index],'after':new[index],
                        'single_bit_difference':(old[index]^new[index]).bit_count()==1} for index in changes[:64]]}
    return report


def capture_macro_snapshot(process,roots):
    player=observe_player(process,roots)
    if not player['player_present']:raise PlayerObservationError('O jogador precisa estar dentro do mundo para comparar o macro.')
    registry=ObjectRegistry(process,roots);registry.allow_widgets(player['roles']['player']['class_address'])
    schema=ReflectionSchema(registry,max_bytes=1048576)
    session=registry.object(player['roles']['session']['address']);selected={}
    for name in ('AutoBattleSystem','QuickSlotSystem'):
        object_=schema.linked(session,'AionCharSession',name)
        if object_ is None or object_['class_name']!=name:raise ReflectionError('Sistema do macro não validado: '+name)
        selected[name]=object_
    game=registry.object(player['roles']['game_instance']['address']);hud=hud_roots(registry,schema,game)
    if len(hud)!=1:raise ReflectionError('HUD do macro não disponível.')
    widget=schema.linked(hud[0],hud[0]['class_name'],'AutoBattle')
    if widget is None or 'AionWidget_HUD_ActionButtonAutoBattle' not in {item['name'] for item in registry.ancestry(widget)}:
        raise ReflectionError('Widget AutoBattle não validado.')
    selected['AutoBattleWidget']=widget;roles={};used=0
    for name,object_ in selected.items():
        size=object_['class_size']
        if not 40<=size<=16384 or size>65536-used:raise ReflectionError('Snapshot de macro excede o limite.')
        used+=size;raw=process.read(object_['address'],size)
        if type(raw) is not bytes or len(raw)!=size:raise ReflectionError('Snapshot de macro incompleto.')
        roles[name]={'identity':(object_['address'],object_['index'],object_['serial']),
                     'class_name':object_['class_name'],'data':raw}
    schema.verify();registry.verify();last=observe_player(process,roots)
    identity=tuple(player['roles']['player'][key] for key in ('address','index','serial'))
    if not last['player_present'] or tuple(last['roles']['player'][key] for key in ('address','index','serial'))!=identity:
        raise PlayerObservationError('Jogador mudou durante o snapshot de macro.')
    return MacroSnapshot(process.session,identity,time.monotonic_ns(),roles)
