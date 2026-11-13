"""Read the current interaction key and resolver with original code checks.

The prompt must belong to the current visible/enabled HUD tree and match both
the selected key and the action resolved by the current input system.
"""
import json
from pathlib import Path
import struct
import time
from .code_anchors import recover_named_anchors, validate_captured_anchors, verify_code_anchors
from .native_player import ObjectRegistry, observe_player
from .reflection_fields import ReflectionSchema, ReflectionError
from .contextual_actions import read_contextual_action_map
from .scene_objects import decode_key
from .native_target import dialog_roots
from .interaction_widgets import matching_interaction_prompts
from .target_code import recover_visibility

REQUIRED_CONTEXTUAL_ANCHORS = ('controls.contextual_interaction_key','controls.contextual_action_resolver')


def recover_contextual_code(process, base):
    definitions=json.loads((Path(base)/'interaction_code_templates.json').read_text('utf-8'))
    return recover_named_anchors(process,definitions,REQUIRED_CONTEXTUAL_ANCHORS)


def read_selected_interaction(schema, session):
    system=schema.linked(session,'AionCharSession','TargetingSystem')
    if system is None:return None,False
    if (system['class_name'] != 'TargetingSystem' or system['class_size'] != 576
            or struct.unpack('<Q',schema.read(system['address']+32,8))[0] != session['address']):
        raise ReflectionError('O sistema de interação possui outro dono ou layout.')
    enabled=schema.read(system['address']+552,1)[0]
    if enabled not in (0,1):raise ReflectionError('Habilitação da interação não é booleana.')
    if not enabled:return None,False
    key=decode_key(schema.read(system['address']+504,24))
    return (key.tag,key.payload,key.extra),True


def read_resolved_action(schema, game):
    system=schema.linked(game,'AionGameInstance','InputSystem')
    if system is None:return None
    if (system['class_name'] != 'AionInputSystem' or system['class_size'] < 656
            or struct.unpack('<Q',schema.read(system['address']+32,8))[0] != game['address']):
        raise ReflectionError('O resolvedor contextual pertence a outra instância ou layout.')
    return read_contextual_action_map(schema.read,system['address']+576).get(66)


def observe_contextual(process, roots, base, anchors=None, visibility=None, visibility_definitions=None):
    definitions=json.loads((Path(base)/'interaction_code_templates.json').read_text('utf-8'))
    if anchors is None:anchors=recover_contextual_code(process,base)
    records=[{'name':name,'rva':rva,'current':raw.hex(),'matches':1} for name,rva,raw in anchors]
    records.append({'session':process.session})
    checked=validate_captured_anchors(process,records,definitions,REQUIRED_CONTEXTUAL_ANCHORS)
    player=observe_player(process,roots)
    result={'session':process.session,'player_present':player['player_present'],
            'game_actions_executed':False,'layout_code_validated':True,
            'prompt_verified':False,'dispatch_eligible':False,
            'selected_key':None,'selection_enabled':False,'resolved_action':None}
    if not player['player_present']:return result
    registry=ObjectRegistry(process,roots)
    schema=ReflectionSchema(registry,max_bytes=2097152)
    session=registry.object(player['roles']['session']['address'])
    game=registry.object(player['roles']['game_instance']['address'])
    key,enabled=read_selected_interaction(schema,session)
    action=read_resolved_action(schema,game)
    matches=()
    if enabled and action in (197,200):
        registry.allow_widgets(player['roles']['player']['class_address'])
        if visibility_definitions is None:
            visibility_definitions=json.loads((Path(base)/'target_templates.json').read_text('utf-8'))
        if visibility is None:visibility=recover_visibility(process,visibility_definitions)
        menus=dialog_roots(registry,schema,game,'HUD_LowLayer')
        if menus:
            matches=matching_interaction_prompts(registry,schema,menus[0],action,key,visibility,visibility_definitions)
    verification_started=time.monotonic()
    schema.verify()
    registry.verify()
    verify_code_anchors(lambda rva,size:process.read(process.base+rva,size),checked)
    verified_at=time.monotonic()
    pawn=player['roles']['player']
    return {**result,'stage':'contextual_key_and_resolver_observed',
            'player_identity':(pawn['index'],pawn['serial']),
            'character_session_identity':(session['index'],session['serial']),
            'selected_key':key,'selection_enabled':enabled,'resolved_action':action,
            'verification_started_at':verification_started,'verified_at':verified_at,
            'prompt_verified':len(matches)==1,'dispatch_eligible':len(matches)==1,
            'prompt':matches[0] if matches else None}
