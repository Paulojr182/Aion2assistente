"""Combine verified inventory and stable bag capacity without guessing other roles.

Domain 1 is the occupancy filter in core.gather_requirements.requirements.
Evidence: native_analysis/inventory_domain_evidence.json. Other domains remain
unnamed; all reviewed domains contribute to totals to avoid counting transfers
between them as new items.
"""
from .inventory_records import InventoryReadError,ITEM_DOMAINS
from .bag_capacity import bag_capacity

PRIMARY_BAG_DOMAIN=1


def merge_bag_inventory(inventory,before,after):
    for field in ('session','player_identity','character_session_identity'):
        values=[report.get(field) for report in (inventory,before,after)]
        if not values[0] or any(value!=values[0] for value in values[1:]):
            raise InventoryReadError('Sessão/personagem diverge entre mochila e inventário.')
    if (inventory.get('player_present') is not True or inventory.get('read_consistency_verified') is not True
            or any(report.get('inventory_capacity_observed') is not True for report in (before,after))
            or any(before.get(field)!=after.get(field) for field in (
                'base_units','paid','bonus','step','maximum_units','capacity'))):
        raise InventoryReadError('Inventário ou capacidade da mochila não permaneceram confirmados.')
    domains=inventory.get('observed_domains',())
    if len(domains)!=len(ITEM_DOMAINS) or set(domains)!={domain for domain,_ in ITEM_DOMAINS}:
        raise InventoryReadError('A leitura não cobre os cinco domínios revisados.')
    fields=('base_units','paid','bonus','step','maximum_units')
    if any(type(before.get(field)) is not int for field in fields):
        raise InventoryReadError('A capacidade não possui seus contadores de origem.')
    expected=bag_capacity(*(before[field] for field in fields))
    capacity=after.get('capacity')
    if type(capacity) is not int or capacity!=expected['capacity']:
        raise InventoryReadError('Capacidade da mochila inválida.')
    if not isinstance(inventory.get('items'),(tuple,list)) or inventory.get('items_truncated') is True:
        raise InventoryReadError('Registros do inventário ausentes ou truncados.')
    used=0;seen=set()
    for row in inventory.get('items',()):
        instance,domain=row.get('instance_id'),row.get('domain')
        if (type(instance) is not int or instance<=0 or instance in seen
                or type(domain) is not int or domain not in {d for d,_ in ITEM_DOMAINS}):
            raise InventoryReadError('Instância ou domínio de inventário inválido.')
        seen.add(instance)
        used+=domain==PRIMARY_BAG_DOMAIN
    if used>capacity:raise InventoryReadError('Ocupação excede a capacidade da mochila.')
    return {**inventory,'inventory_capacity_observed':True,'capacity':capacity,
            'used_slots':used,'free_slots':capacity-used,
            'primary_bag_domain_classified':True,'primary_bag_domain':PRIMARY_BAG_DOMAIN,
            'storage_roles':{str(PRIMARY_BAG_DOMAIN):'primary_bag'},
            'storage_roles_classified':False,
            'unclassified_domains':[domain for domain,_ in ITEM_DOMAINS if domain!=PRIMARY_BAG_DOMAIN]}
