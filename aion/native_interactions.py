"""Read-only interaction chain grounded in the authenticated original profile.

The native observer checks the interaction-start/lifecycle instruction patterns
in this client. Contextual prompt/selection authorization is still separate.
"""
import json
from pathlib import Path
import struct
from .native_player import ObjectRegistry, observe_player
from .reflection_fields import ReflectionSchema, ReflectionError
from .scene_objects import decode_key
from .code_anchors import recover_named_anchors, validate_captured_anchors, verify_code_anchors

REQUIRED_INTERACTION_ANCHORS = ('environment.interaction_start','environment.action_lifecycle')


def recover_interaction_code(process, base):
    definitions=json.loads((Path(base)/'interaction_code_templates.json').read_text('utf-8'))
    return recover_named_anchors(process,definitions,REQUIRED_INTERACTION_ANCHORS)


def read_interaction_chain(registry, schema, pawn, *, limit=64, code_verified=False):
    if type(limit) is not int or not 0 < limit <= 64:
        raise ReflectionError('Limite de cadeia de interação inválido.')
    if 'AionCharacter' not in {a['name'] for a in registry.ancestry(pawn)}:
        raise ReflectionError('Interações exigem personagem nativo.')
    current = schema.linked(pawn, 'AionCharacter', 'RootAction')
    previous = None
    seen = set()
    rows = []
    while current is not None:
        identity = current['index'], current['serial']
        ancestry = {a['name'] for a in registry.ancestry(current)}
        if identity in seen or len(seen) >= limit or 'AionCharacterActionBase' not in ancestry:
            raise ReflectionError('Cadeia de ações cíclica, extensa ou inválida.')
        seen.add(identity)
        owner = struct.unpack('<iI', schema.read(current['address']+40, 8))
        if owner != (pawn['index'], pawn['serial']):
            raise ReflectionError('A interação pertence a outro personagem.')
        field = schema.field(current, 'AionCharacterActionBase', 'SuperAction')
        if field.kind != 'WeakObjectProperty' or field.count != 1 or field.stride != 8:
            raise ReflectionError('Vínculo de ação superior possui outro tipo.')
        schema.default_accessors(field)
        parent = struct.unpack('<iI', schema.read(current['address']+field.offset, 8))
        if previous is None:
            if parent[0] >= 0 and parent[1] != 0:
                raise ReflectionError('A ação raiz possui uma ação superior inesperada.')
        elif parent != previous:
            raise ReflectionError('Ação superior mudou durante a leitura.')
        if 'AionCharacterAction_Interact' in ancestry:
            if current['class_size'] < 144:
                raise ReflectionError('A interação não comporta o layout recuperado.')
            raw = schema.read(current['address']+64, 80)
            if raw[5] not in (0, 1) or raw[6] not in (0, 1):
                raise ReflectionError('Flags de ciclo da interação inválidas.')
            key = decode_key(raw[56:80])
            rows.append({'object_index': identity[0], 'serial_number': identity[1],
                         'client_started': bool(raw[5]), 'client_ended': bool(raw[6]),
                         'target': (key.tag, key.payload, key.extra),
                         'layout_code_validated': code_verified is True})
        previous = identity
        current = schema.linked(current, 'AionCharacterActionBase', 'SubAction')
    return tuple(rows)


def observe_interactions(process, roots, base, anchors=None):
    definitions=json.loads((Path(base)/'interaction_code_templates.json').read_text('utf-8'))
    if anchors is None:anchors=recover_interaction_code(process,base)
    records=[{'name':name,'rva':rva,'current':raw.hex(),'matches':1} for name,rva,raw in anchors]
    records.append({'session':process.session})
    checked=validate_captured_anchors(process,records,definitions,REQUIRED_INTERACTION_ANCHORS)
    player = observe_player(process, roots)
    result = {'session': process.session, 'player_present': player['player_present'],
              'game_actions_executed': False, 'layout_code_validated': True,
              'receipt_eligible': False, 'actions': []}
    if not player['player_present']:return result
    registry = ObjectRegistry(process, roots)
    schema = ReflectionSchema(registry, max_bytes=1048576)
    pawn = registry.object(player['roles']['player']['address'])
    actions = read_interaction_chain(registry, schema, pawn, code_verified=True)
    schema.verify()
    registry.verify()
    verify_code_anchors(lambda rva,size:process.read(process.base+rva,size),checked)
    return {**result, 'stage': 'interaction_chain_verified', 'receipt_eligible': True,
            'player_identity': (pawn['index'], pawn['serial']),
            'character_session_identity': (player['roles']['session']['index'],player['roles']['session']['serial']),
            'actions': actions}
