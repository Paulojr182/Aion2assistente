"""Current-session quest index/progress observation with code verification."""
import json
from pathlib import Path
from dataclasses import asdict
from .native_player import ObjectRegistry, observe_player
from .reflection_fields import ReflectionSchema
from .quest_records import read_quest_records, QuestRecordsError
from .code_anchors import validate_captured_anchors, verify_code_anchors, recover_named_anchors

REQUIRED_QUEST_ANCHORS = tuple('steam_host.retail_quest_profile:'+hex(rva) for rva in (
    148547903, 148549088, 148571433, 148571600, 148571730, 148571824))


def recover_quest_code(process, base):
    definitions = json.loads((Path(base)/'activity_code_templates.json').read_text('utf-8'))
    return recover_named_anchors(process, definitions, REQUIRED_QUEST_ANCHORS)


def observe_quests(process, roots, base, anchors=None):
    definitions = json.loads((Path(base)/'activity_code_templates.json').read_text('utf-8'))
    if anchors is None:anchors = recover_quest_code(process, base)
    records = [{'name':name,'rva':rva,'current':raw.hex(),'matches':1} for name,rva,raw in anchors]
    records.append({'session':process.session})
    anchors = validate_captured_anchors(process, records, definitions, REQUIRED_QUEST_ANCHORS)
    player = observe_player(process, roots)
    if not player['player_present']:
        verify_code_anchors(lambda rva,size:process.read(process.base+rva,size), anchors)
        return {'session': process.session, 'player_present': False, 'game_actions_executed': False, 'records': []}
    scope={'player_identity':(player['roles']['player']['index'],player['roles']['player']['serial']),
           'character_session_identity':(player['roles']['session']['index'],player['roles']['session']['serial'])}
    registry = ObjectRegistry(process, roots)
    schema = ReflectionSchema(registry)
    session = registry.object(player['roles']['session']['address'])
    system = schema.linked(session, 'AionCharSession', 'QuestSystem')
    if system is None:
        schema.verify()
        registry.verify()
        verify_code_anchors(lambda rva,size:process.read(process.base+rva,size), anchors)
        return {**scope,'session': process.session, 'player_present': True, 'stage': 'quest_system_absent', 'records': [],
                'game_actions_executed': False}
    if system['class_name'] != 'QuestSystem' or system['class_size'] < 128:
        raise QuestRecordsError('Classe do sistema de missões inválida.')
    quests = read_quest_records(process.read, system['address'], session['address'])
    schema.verify()
    registry.verify()
    verify_code_anchors(lambda rva,size:process.read(process.base+rva,size), anchors)
    return {**scope,'player_present': True, 'session': process.session,
            'stage': 'quest_index_and_progress_observed', 'game_actions_executed': False,
            'definition_ids_resolved': bool(quests) and all(q.definition_id is not None for q in quests),
            'definition_names_resolved': False, 'state_semantics_resolved': False,
            'code_anchors_verified': len(anchors),
            'records': [asdict(record) for record in quests]}
