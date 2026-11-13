"""Read-only current-character bag capacity after checking its native code."""
import json
from pathlib import Path
import struct
from .bag_capacity import accessor_root,read_bag_capacity
from .code_anchors import recover_named_anchors,validate_captured_anchors,verify_code_anchors
from .inventory_records import InventoryReadError
from .native_player import ObjectRegistry,observe_player
from .reflection_fields import ReflectionSchema
from .metadata_templates import InstructionTemplate

REQUIRED_BAG_ANCHORS=('controls.bag_capacity','controls.bag_config_accessor','controls.bag_table_accessor')


def recover_bag_code(process,base):
    definitions=json.loads((Path(base)/'bag_code_templates.json').read_text('utf-8'))
    entry=recover_named_anchors(process,definitions,(REQUIRED_BAG_ANCHORS[0],))
    addresses=linked_bag_accessors(definitions,entry)
    anchors=list(entry)
    for name in REQUIRED_BAG_ANCHORS[1:]:
        selected=[d for d in definitions if d['name']==name]
        if len(selected)!=1:raise InventoryReadError('Modelo do acessor da mochila ausente ou duplicado.')
        definition=selected[0];rva=addresses[name]
        template=InstructionTemplate(name,definition['old_rva'],bytes.fromhex(definition['expected']),
            tuple(tuple(p) for p in definition['operands']))
        if not 0<=rva<=process.image_size-len(template.expected):
            raise InventoryReadError('Acessor chamado pela mochila fora da imagem.')
        raw=process.read(process.base+rva,len(template.expected))
        template.targets(raw,rva,process.image_size)
        anchors.append((name,rva,raw))
    verify_code_anchors(lambda rva,size:process.read(process.base+rva,size),anchors)
    return tuple(anchors)


def linked_bag_accessors(definitions,anchors):
    body_name=REQUIRED_BAG_ANCHORS[0]
    sources={}
    for name in REQUIRED_BAG_ANCHORS:
        matches=[d for d in definitions if d['name']==name]
        if len(matches)!=1:raise InventoryReadError('Modelos da mochila incompletos ou duplicados.')
        sources[name]=matches[0]
    entries=[a for a in anchors if a[0]==body_name]
    if len(entries)!=1:raise InventoryReadError('Corpo nativo da mochila ausente ou ambíguo.')
    _,rva,raw=entries[0];source=sources[body_name];old=bytes.fromhex(source['expected'])
    result={}
    for name in REQUIRED_BAG_ANCHORS[1:]:
        linked=[]
        for offset,end in source['operands']:
            old_target=source['old_rva']+end+struct.unpack_from('<i',old,offset)[0]
            if old_target==sources[name]['old_rva']:
                linked.append(rva+end+struct.unpack_from('<i',raw,offset)[0])
        if not linked or len(set(linked))!=1:
            raise InventoryReadError('Chamadas da mochila não concordam sobre seu acessor.')
        result[name]=linked[0]
    return result


def observe_bag_state(process,roots,base,anchors=None):
    definitions=json.loads((Path(base)/'bag_code_templates.json').read_text('utf-8'))
    if anchors is None:anchors=recover_bag_code(process,base)
    records=[{'name':name,'rva':rva,'current':raw.hex(),'matches':1} for name,rva,raw in anchors]
    records.append({'session':process.session})
    checked=validate_captured_anchors(process,records,definitions,REQUIRED_BAG_ANCHORS)
    indexed={name:(rva,raw) for name,rva,raw in checked}
    for name,rva in linked_bag_accessors(definitions,checked).items():
        if indexed[name][0]!=rva:raise InventoryReadError('Acessor não pertence ao corpo nativo da mochila.')
    config=accessor_root(*indexed['controls.bag_config_accessor'],process.image_size)+process.base
    table=accessor_root(*indexed['controls.bag_table_accessor'],process.image_size)+process.base
    player=observe_player(process,roots)
    result={'session':process.session,'player_present':player['player_present'],
            'game_actions_executed':False,'inventory_capacity_observed':False,
            'storage_roles_classified':False}
    if not player['player_present']:return result
    registry=ObjectRegistry(process,roots)
    schema=ReflectionSchema(registry,max_bytes=1048576)
    session=registry.object(player['roles']['session']['address'])
    game=registry.object(player['roles']['game_instance']['address'])
    if session['class_name']!='AionCharSession' or session['class_size']<1874:
        raise InventoryReadError('A sessão não comporta os contadores da mochila.')
    current=schema.linked(game,'AionGameInstance','CharSession',1072)
    if current is None or (current['index'],current['serial'])!=(session['index'],session['serial']):
        raise InventoryReadError('A sessão da mochila mudou.')
    system=schema.linked(session,'AionCharSession','ItemSystem',80)
    if (system is None or system['class_name']!='ItemSystem' or system['class_size']<40
            or struct.unpack('<Q',schema.read(system['address']+32,8))[0]!=session['address']):
        raise InventoryReadError('A mochila pertence a outra sessão ou sistema.')
    state=read_bag_capacity(schema.read,session['address'],config,table)
    schema.verify();registry.verify()
    verify_code_anchors(lambda rva,size:process.read(process.base+rva,size),checked)
    pawn=player['roles']['player']
    return {**result,**state,'stage':'bag_capacity_observed','inventory_capacity_observed':True,
            'player_identity':(pawn['index'],pawn['serial']),
            'character_session_identity':(session['index'],session['serial'])}
