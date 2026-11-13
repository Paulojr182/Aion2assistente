"""Read current native environment/gather definitions and resource positions."""
import json
import math
from pathlib import Path
import struct
from dataclasses import dataclass
from .native_player import ObjectRegistry, observe_player
from .native_npc_schema import find_npc_declarations
from .reflection_fields import ReflectionSchema, ReflectionError
from .native_scene import observe_scene
from .native_positions import actor_position
from .native_u32_table import enumerate_u32_rows, lookup_u32_row, TableReadError
from .code_anchors import validate_captured_anchors, verify_code_anchors, recover_named_anchors
from .metadata_templates import InstructionTemplate
from .table_accessor import decode_table_accessor

REQUIRED_GATHER_SOURCE_ANCHORS = (
    'steam_host.retail_gather_profile:0x9e7eb5a',
    'steam_host.retail_gather_profile:0x9e7f1de',
    'steam_host.retail_gather_profile:0x9e7f241',
    'steam_host.retail_environment_profile:0x93ad212')


@dataclass(frozen=True)
class GatherSourceProfile:
    session: str
    anchors: tuple
    tables: dict


def recover_gather_source_profile(process, base):
    definitions = json.loads((Path(base)/'activity_code_templates.json').read_text('utf-8'))
    anchors = recover_named_anchors(process, definitions, REQUIRED_GATHER_SOURCE_ANCHORS)
    tables = {}
    for name, key in (('gather_sources','steam_host.retail_gather_profile:0x9e7f1de'),
                      ('environment_definitions','steam_host.retail_environment_profile:0x93ad212')):
        _, rva, raw = next(a for a in anchors if a[0] == key)
        source = next(d for d in definitions if d['name'] == key)
        template = InstructionTemplate(key, source['old_rva'], bytes.fromhex(source['expected']),
            tuple(tuple(p) for p in source['operands']))
        targets = template.targets(raw, rva, process.image_size)
        if not targets:raise TableReadError('A assinatura não identifica o acessor de tabela.')
        accessor = targets[0]
        if not 0 <= accessor <= process.image_size-1024:
            raise TableReadError('Acessor fora da imagem do cliente.')
        code = process.read(process.base+accessor, 1024)
        tables[name] = decode_table_accessor(code, accessor, process.image_size)
        if process.read(process.base+accessor, 1024) != code:
            raise TableReadError('Acessor mudou durante a recuperação.')
    verify_code_anchors(lambda rva,size:process.read(process.base+rva,size), anchors)
    return GatherSourceProfile(process.session, anchors, tables)

PROFESSIONS = {
    1: ('Metal', 'Ore'),
    2: ('RareMetal', 'Ore'),
    3: ('Jewelry', 'Gems'),
    4: ('Tree', 'Wood'),
    5: ('Flower', 'Herbs'),
    6: ('Herb', 'Herbs'),
    7: ('Berry', 'Food'),
    8: ('Vegetable', 'Food'),
    9: ('Shellfish', 'Food'),
    10: ('Od', 'Od'),
}


def gather_profession(source):
    kind = source.get('source_type') if isinstance(source, dict) else None
    if not isinstance(kind, dict):
        raise TableReadError('Tipo de coleta nativo ausente.')
    expected = PROFESSIONS.get(kind.get('raw'))
    if (expected is None or kind.get('declaration') != 'EGatherSourceType'
            or kind.get('symbol') != 'EGatherSourceType::'+expected[0]):
        raise TableReadError('Tipo de coleta nativo sem filtro de profissão revisado.')
    return expected[1]


def gather_mastery_requirement(source, mastery):
    required = source.get('mastery_level') if isinstance(source, dict) else None
    if type(required) is not int or not 0 <= required < 4294967296:
        raise TableReadError('O recurso possui requisito de maestria inválido.')
    if not isinstance(mastery, dict) or mastery.get('stage') != 'mastery_observed':
        return None
    level = mastery.get('level')
    if type(level) is not int or not 0 < level < 4294967296:
        raise TableReadError('O nível atual de maestria de coleta é inválido.')
    return required <= level


def read_gather_mastery(schema, session):
    if session['class_name'] != 'AionCharSession' or session['class_size'] < 5340:
        raise TableReadError('Sessão não comporta o nível de maestria validado.')
    system = schema.linked(session, 'AionCharSession', 'GatherSystem', 216)
    if system is None or system['class_name'] != 'GatherSystem' or system['class_size'] < 40:
        raise TableReadError('Sistema de coleta ausente ou inválido na sessão.')
    if struct.unpack('<Q', schema.read(system['address']+32, 8))[0] != session['address']:
        raise TableReadError('O sistema de coleta pertence a outro personagem.')
    level = struct.unpack('<I', schema.read(session['address']+5336, 4))[0]
    if level == 0:
        return {'stage': 'level_uninitialized', 'level': 0}
    return {'stage': 'mastery_observed', 'level': level}


def observe_gather_sources(process, roots, base, profile=None):
    base = Path(base)
    if profile is None:profile = recover_gather_source_profile(process, base)
    if profile.session != process.session:
        raise TableReadError('Revalide os acessores de recursos para esta sessão.')
    anchors = [{'name':name,'rva':rva,'current':raw.hex(),'matches':1} for name,rva,raw in profile.anchors]
    anchors.append({'session':process.session})
    definitions_code = json.loads((base/'activity_code_templates.json').read_text('utf-8'))
    checked_code = validate_captured_anchors(process, anchors, definitions_code, REQUIRED_GATHER_SOURCE_ANCHORS)
    tables = {}
    for name, data in profile.tables.items():
        raw = bytes.fromhex(data['code'])
        if process.read(process.base+data['rva'], len(raw)) != raw:
            raise TableReadError('Acessor de tabela mudou.')
        tables[name] = process.base+data['root_rva'], process.base+data['vtable_rva']
    player = observe_player(process, roots)
    if not player['player_present']:
        return {'player_present': False, 'game_actions_executed': False, 'targets': []}
    registry = ObjectRegistry(process, roots)
    schema = ReflectionSchema(registry, max_bytes=2097152)
    declarations = {}
    for declaration, size, hint in (('GatherSourceTableItem', 64, 17988), ('EnvObjTableItem', 352, 17900)):
        matches = []
        try:
            address, _ = registry.slot(hint)
            obj = registry.object(address)
            header = process.read(address, 96)
            name = registry.names.resolve(struct.unpack_from('<I', header, 24)[0])
            if obj['class_name'] == 'ScriptStruct' and name == declaration:
                matches = [obj]
        except (ValueError, OSError):
            pass
        if not matches:
            matches, _, _, _ = find_npc_declarations(registry, {'name': declaration},
                                                     max_bytes=12582912, max_slots=65536)
        if len(matches) != 1:
            raise ReflectionError('Declaração de recurso ausente ou ambígua: '+declaration)
        obj = matches[0]
        raw = schema.read(obj['address'], 96)
        extent, alignment = struct.unpack_from('<ii', raw, 88)
        if (extent, alignment) != (size, 8):
            raise ReflectionError('Layout da declaração de recurso mudou: '+declaration)
        fields = schema.own_fields(obj['address'], size, struct.unpack_from('<Q', raw, 80)[0])
        declarations[declaration] = obj['index'], {f.name: f for f in fields}
    def field(declaration, name, kind, offset, stride):
        value = declarations[declaration][1].get(name)
        if value is None or (value.kind, value.offset, value.stride, value.count, value.number) != (kind, offset, stride, 1, 0):
            raise ReflectionError('Campo de recurso mudou: '+name)
        schema.default_accessors(value)
        return value
    field('GatherSourceTableItem', 'ID', 'StructProperty', 8, 4)
    field('GatherSourceTableItem', 'Name', 'NameProperty', 12, 8)
    field('GatherSourceTableItem', 'MaterialGroup', 'NameProperty', 20, 8)
    source_type_field = field('GatherSourceTableItem', 'SourceType', 'EnumProperty', 28, 1)
    source_grade_field = field('GatherSourceTableItem', 'SourceGrade', 'EnumProperty', 29, 1)
    field('GatherSourceTableItem', 'MasteryLevel', 'UInt32Property', 32, 4)
    field('GatherSourceTableItem', 'ConversionRecipe', 'NameProperty', 36, 8)
    field('GatherSourceTableItem', 'QuestStepId', 'StructProperty', 44, 4)
    field('GatherSourceTableItem', 'QuestMaterial', 'NameProperty', 48, 8)
    field('GatherSourceTableItem', 'QuestMaterialCount', 'UInt32Property', 56, 4)
    usage = field('EnvObjTableItem', 'Usage', 'EnumProperty', 52, 1)
    field('EnvObjTableItem', 'UsageValue', 'NameProperty', 56, 8)
    field('EnvObjTableItem', 'InteractRange', 'UInt32Property', 92, 4)
    enum_maps = {}
    for enum_field, expected_name in ((source_type_field, 'EGatherSourceType'),
                                      (source_grade_field, 'EGatherSourceGrade')):
        values = schema.enum_values(enum_field)
        if values['name'] != expected_name:
            raise ReflectionError('Domínio de enum de coleta mudou: '+expected_name)
        enum_maps[expected_name] = {int(e['value_decimal']): e['name'] for e in values['entries']}
    usage_values = schema.enum_values(usage)
    gather_values = {int(e['value_decimal']) for e in usage_values['entries']
                     if e['name'] == 'EEnvObjectUsage::GatherSource'}
    if len(gather_values) != 1:
        raise ReflectionError('Semântica de coleta do enum não foi confirmada.', details=usage_values)
    def decode_enum(domain, raw_byte):
        symbol = enum_maps[domain].get(raw_byte)
        if not symbol or symbol.endswith('::Max'):
            raise TableReadError('Valor de enum de coleta inválido: '+domain)
        return {'declaration': domain, 'raw': raw_byte, 'symbol': symbol}
    session_obj = registry.object(player['roles']['session']['address'])
    mastery = read_gather_mastery(schema, session_obj)
    root, vtable = tables['gather_sources']
    sources = {}
    for key, row, _ in enumerate_u32_rows(process.read, root, expected_vtable=vtable):
        raw = process.read(row, 64)
        name_pair = struct.unpack_from('<II', raw, 12)
        if name_pair in sources:
            raise TableReadError('Nome de fonte de coleta ambíguo.')
        source_type = decode_enum('EGatherSourceType', raw[28])
        grade = decode_enum('EGatherSourceGrade', raw[29])
        source_entry = {
            'id': key,
            'name': registry.names.resolve(name_pair[0]),
            'material_group': registry.names.resolve(struct.unpack_from('<I', raw, 20)[0]),
            'source_type': source_type,
            'grade': grade,
            'mastery_level': struct.unpack_from('<I', raw, 32)[0],
            'conversion_recipe': registry.names.resolve(struct.unpack_from('<I', raw, 36)[0]),
            'quest_step_id': struct.unpack_from('<I', raw, 44)[0],
            'quest_material': registry.names.resolve(struct.unpack_from('<I', raw, 48)[0]),
            'quest_material_count': struct.unpack_from('<I', raw, 56)[0],
        }
        source_entry['profession'] = gather_profession(source_entry)
        sources[name_pair] = source_entry
    scene = observe_scene(process, roots, report_limit=1024, known_keys_only=True)
    pawn = registry.object(player['roles']['player']['address'])
    position = actor_position(registry, schema, pawn)
    seen = set()
    targets = []
    root, vtable = tables['environment_definitions']
    for collection in scene['collections']:
        for actor in collection['entries']:
            identity = actor['index'], actor['serial']
            if identity in seen or 'AionEnvObj' not in actor['ancestry']:
                continue
            seen.add(identity)
            if actor['class_size'] < 1424:
                raise ReflectionError('Ator de ambiente não comporta o vínculo validado.')
            info_bytes = process.read(actor['address']+1416, 8)
            info = struct.unpack('<Q', info_bytes)[0]
            if not info:
                continue
            definition_bytes = process.read(info+40, 4)
            definition_id = struct.unpack('<I', definition_bytes)[0]
            found = lookup_u32_row(process.read, root, definition_id, expected_vtable=vtable)
            if found is None:
                continue
            definition = process.read(found[0], 232)
            if struct.unpack_from('<I', definition, 8)[0] != definition_id:
                raise TableReadError('ID da definição de ambiente diverge do ator.')
            if definition[52] not in gather_values:
                continue
            source = sources.get(struct.unpack_from('<II', definition, 56))
            if source is None:
                continue
            target_position = actor_position(registry, schema, actor)
            distance = None
            if (position['position_observed'] and target_position['position_observed']
                    and position['world'] == target_position['world']):
                p1 = position['position']
                p2 = target_position['position']
                dz = abs(p1[2] - p2[2])
                if source.get('profession') != 'Od' and dz <= 300.0:
                    distance = math.hypot(p1[0] - p2[0], p1[1] - p2[1])
                else:
                    distance = math.dist(p1, p2)
            if (process.read(actor['address']+1416, 8) != info_bytes
                    or process.read(info+40, 4) != definition_bytes
                    or process.read(found[0], 232) != definition):
                raise TableReadError('Recurso mudou durante a leitura.')
            targets.append({'identity': identity, 'environment_key': actor['key_payload_decimal'],
                            'object_key': (actor['key_tag'],int(actor['key_payload_decimal']),actor['key_extra']),
                            'definition_id': definition_id, 'source': source,
                            'profession': source['profession'],
                            'mastery_requirement_met': gather_mastery_requirement(source, mastery),
                            'interaction_range_engine_units': struct.unpack_from('<I', definition, 92)[0],
                            'distance_engine_units': distance, 'position': target_position})
    schema.verify()
    registry.verify()
    verify_code_anchors(lambda rva,size:process.read(process.base+rva,size), checked_code)
    for data in profile.tables.values():
        raw = bytes.fromhex(data['code'])
        if process.read(process.base+data['rva'],len(raw)) != raw:
            raise TableReadError('Acessor mudou antes da publicação de recursos.')
    return {'player_present': True, 'session': process.session, 'game_actions_executed': False,
            'player_identity': (pawn['index'], pawn['serial']),
            'character_session_identity': (session_obj['index'], session_obj['serial']),
            'stage': 'native_gather_definitions_and_targets_observed', 'mastery': mastery,
            'player_position': position, 'targets': targets, 'catalog_count': len(sources),
            'declaration_indices': {k: v[0] for k, v in declarations.items()}}
