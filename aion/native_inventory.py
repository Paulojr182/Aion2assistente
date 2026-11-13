"""Verified current-session native inventory records; no storage-role guesses."""
import struct
from dataclasses import asdict
from .native_player import ObjectRegistry, observe_player
from .reflection_fields import ReflectionSchema
from .inventory_records import ITEM_DOMAINS, InventoryReadError, read_item_domain
from .code_anchors import verify_code_anchors


def observe_inventory(process, roots, anchors):
    required = {'steam_host.retail_inventory_profile:'+hex(rva) for rva in (
        145335744, 147706768, 147816432, 149607744, 149607936, 149608480, 149608672)}
    if len(anchors) != 7 or {a[0] for a in anchors} != required:
        raise InventoryReadError('Perfil de código de inventário incompleto.')
    read_code = lambda rva, size: process.read(process.base+rva, size)
    verify_code_anchors(read_code, anchors)
    player = observe_player(process, roots)
    result = {'session': process.session, 'player_present': player['player_present'],
              'game_actions_executed': False, 'storage_roles_classified': False,
              'inventory_capacity_observed': False, 'atomic_snapshot': False,
              'items': []}
    if not player['player_present']:return result
    registry = ObjectRegistry(process, roots)
    schema = ReflectionSchema(registry, max_bytes=4194304)
    session = registry.object(player['roles']['session']['address'])
    system = schema.linked(session, 'AionCharSession', 'ItemSystem', 80)
    if system is None:
        return {**result, 'stage': 'item_system_absent'}
    if (system['class_name'] != 'ItemSystem' or system['class_size'] != 1560
            or session['class_name'] != 'AionCharSession' or session['class_size'] != 38392
            or struct.unpack('<Q', schema.read(system['address']+32, 8))[0] != session['address']):
        raise InventoryReadError('Dono, classe ou extensão do inventário mudou.')
    rows = [];quantities=[]
    for mask, offset in ITEM_DOMAINS:
        rows.extend(read_item_domain(schema.read, process.read, session['address']+offset, mask,
            quantity_journal=quantities))
    if len(rows) > 8192 or len({r.instance_id for r in rows}) != len(rows):
        raise InventoryReadError('Instâncias repetidas entre domínios ou limite total excedido.')
    for address,expected in quantities:
        if process.read(address,len(expected))!=expected:
            raise InventoryReadError('Quantidade mudou durante a leitura dos domínios.')
    schema.verify()
    registry.verify()
    verify_code_anchors(read_code, anchors)
    return {**result, 'stage': 'item_domains_observed',
            'player_identity': (player['roles']['player']['index'], player['roles']['player']['serial']),
            'character_session_identity': (session['index'], session['serial']),
            'observed_domains': [domain for domain,_ in ITEM_DOMAINS],
            'read_consistency_verified': True,
            'items': [asdict(row) for row in rows]}


def observe_inventory_with_bag(process,roots,base,anchors,bag_anchors):
    from .native_bag import observe_bag_state
    from .bag_inventory import merge_bag_inventory
    before=observe_bag_state(process,roots,base,bag_anchors)
    inventory=observe_inventory(process,roots,anchors)
    after=observe_bag_state(process,roots,base,bag_anchors)
    return merge_bag_inventory(inventory,before,after)
