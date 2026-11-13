"""Friendly in-game display name resolution for AION 2 gatherables and monsters."""

GATHER_NAME_MAP = {
    'gather_source_food_l_lv1_02': 'Coriolus',
    'gather_source_food_l_lv1_01': 'Coriolus',
    'gather_source_food': 'Coriolus',
    'gather_od_lv1_common_001_f': 'Od',
    'gather_od': 'Od',
    'gather_gemstone_lv1_rare_002_f': 'Gema (Gemstone)',
    'gather_gemstone_lv1_common_001_f': 'Minério (Ore)',
    'gather_gemstone': 'Gema (Gemstone)',
    'gather_metal': 'Minério de Metal',
    'gather_herb': 'Erva Silvestre',
    'gather_tree': 'Madeira',
}

MONSTER_NAME_MAP = {
    'swampman': 'Targon do Pântano',
    'swampspider': 'Aranha de Od do Pântano',
    'spider': 'Aranha de Od do Pântano',
    'drakanhound': 'Cão de Guarda dos Drakan',
    'drakandefender': 'Defensor Drakan de Nahid',
    'drakanassault': 'Soldado de Assalto Drakan de Nahid',
    'drakan': 'Guarda Drakan',
}


def resolve_gather_name(raw_name: str) -> str:
    if not raw_name:
        return 'Recurso'
    clean = raw_name.lower().strip()
    if clean in GATHER_NAME_MAP:
        return GATHER_NAME_MAP[clean]
    for key, display in GATHER_NAME_MAP.items():
        if key in clean:
            return display
    return raw_name.replace('Gather_', '').replace('_', ' ').strip()


def resolve_monster_name(internal_name: str, resource_name: str = '', level: int = None) -> str:
    candidates = [c for c in (internal_name, resource_name) if c]
    for c in candidates:
        low = c.lower()
        for key, display in MONSTER_NAME_MAP.items():
            if key in low:
                return f"{display} (Nv. {level})" if level else display
    raw = internal_name or resource_name or 'Monstro'
    clean = raw
    for prefix in ('M_PD_', 'Basic_M_', 'M_L1_', 'M_L2_', 'M_L3_', 'M_', 'MOB_'):
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
    clean = clean.replace('_01', '').replace('_02', '').replace('_Grade1', '').replace('_Grade2', '').replace('_Grade3', '').replace('_', ' ').strip()
    return f"{clean} (Nv. {level})" if level else clean
