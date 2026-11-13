"""Read-only native gather HUD progress, result animations, and attempt tracking."""
from dataclasses import dataclass
import json
from pathlib import Path
import struct
from .native_player import ObjectRegistry, observe_player
from .native_target import hud_roots
from .reflection_fields import ReflectionSchema, ReflectionError
from .target_code import widget_visibility
from .code_anchors import validate_captured_anchors, verify_code_anchors, recover_named_anchors
from .name_pool import pointer as checked_pointer,NamePoolError

SEQUENCE_GETTER_BYTES = b'\x8b\x81\xc4\x02\x00\x00\xc3'
REQUIRED_GATHER_PROGRESS_ANCHORS = (
    'steam_host.retail_gather_profile:0x9e7edb2',
    'steam_host.retail_gather_profile:0x9e7ee37',
    'steam_host.retail_gather_profile:0x9e7f194',
    'steam_host.retail_gather_profile:0x9e7f91c',
)
HUD_GATHER_PROGRESS_LAYOUTS={1344:968,2288:2080}


def gather_progress_result(stage, source_id, finalizing, success, failure):
    if stage != 'progress_visible':
        return None
    if not all(type(value) is bool for value in (finalizing, success, failure)):
        raise ReflectionError('As flags de resultado de coleta devem ser booleanos observados.')
    if type(source_id) is not int or not 0 < source_id < 4294967296:
        raise ReflectionError('O resultado de coleta visível não possui ID de fonte nativo.')
    if success and failure:
        raise ReflectionError('O HUD de coleta possui animações de resultado conflitantes.')
    if not finalizing:
        return None
    if success:
        return 'success'
    if failure:
        return 'failure'
    return None


@dataclass(frozen=True)
class GatherProgressFrame:
    session: str
    stage: str
    source_id: int | None = None
    finalizing: bool | None = None
    success_animation_playing: bool | None = None
    failure_animation_playing: bool | None = None

    def result_kind(self):
        return gather_progress_result(
            self.stage,
            self.source_id,
            self.finalizing,
            self.success_animation_playing,
            self.failure_animation_playing,
        )


def gather_sequence_element(schema,field):
    matches=[]
    for offset in (112,120):
        address=struct.unpack('<Q',schema.read(field.address+offset,8))[0]
        if not address:continue
        try:checked_pointer(address)
        except NamePoolError:continue
        raw=schema.read(address,72)
        type_pointer,owner=struct.unpack_from('<QQ',raw,8)
        if owner!=field.address:continue
        count,stride=struct.unpack_from('<ii',raw,48)
        field_offset=struct.unpack_from('<i',raw,68)[0]
        kind=schema.type_info(type_pointer)
        if ((count,stride,field_offset)!=(1,8,0) or kind not in (
                ('ObjectProperty',65536,67207169),
                ('ObjectProperty',9007199254740992,9007199321948161))):
            raise ReflectionError('O descritor filho de animações possui outro tipo ou extensão.')
        declared_class=struct.unpack('<Q',schema.read(address+112,8))[0]
        if schema.registry.class_info(declared_class)['name']!='UMGSequencePlayer':
            raise ReflectionError('O array de animações declara outra classe.')
        matches.append(address)
    if len(matches)!=1:raise ReflectionError('Descritor de animações ausente ou ambíguo.')
    return matches[0]


def gather_player_animation(schema,player):
    field=schema.field(player,'UMGSequencePlayer','Animation')
    if ((field.kind,field.count,field.stride,field.number)!=('ObjectProperty',1,8,0)
            or field.offset not in (544,816)):
        raise ReflectionError('O vínculo de animação do sequence player possui outro contrato.')
    return schema.linked(player,'UMGSequencePlayer','Animation',field.offset)


def read_gather_result_animations(schema, widget):
    animations = {}
    for name, offset in (('Anim_CompleteSuccess', 1272), ('Anim_CompleteFail', 1280)):
        anim = schema.linked(widget, 'AionWidget_HUD_GatherProgress', name, offset)
        if anim is None or anim['class_name'] != 'WidgetAnimation':
            raise ReflectionError('O HUD de coleta não possui a animação de resultado esperada: '+name)
        animations[name] = anim
    if (animations['Anim_CompleteSuccess']['index'], animations['Anim_CompleteSuccess']['serial']) == (
            animations['Anim_CompleteFail']['index'], animations['Anim_CompleteFail']['serial']):
        raise ReflectionError('Sucesso e falha devem identificar animações diferentes.')
    field = schema.field(widget, 'UserWidget', 'ActiveSequencePlayers')
    if (field.kind, field.offset, field.count, field.stride, field.type_id, field.cast_flags) != (
            'ArrayProperty', 544, 1, 16, 2097152, 2129921):
        raise ReflectionError('O layout de sequências de animação ativas mudou.')
    schema.default_accessors(field)
    gather_sequence_element(schema,field)
    pointer, count, capacity = struct.unpack('<Qii', schema.read(widget['address']+544, 16))
    if not 0 <= count <= capacity <= 128 or (count and (not pointer or pointer % 8)):
        raise ReflectionError('A contagem de sequências de animação de coleta excede o limite.')
    playing = set()
    if count:
        slots = struct.unpack('<'+'Q'*count, schema.read(pointer, count*8))
        seen_players = set()
        for player_addr in slots:
            if not player_addr or player_addr in seen_players:
                raise ReflectionError('A lista de animações de coleta repete ou omite um sequence player.')
            seen_players.add(player_addr)
            player = schema.registry.object(player_addr)
            if (player['class_name'], player['class_size']) != ('UMGSequencePlayer', 832):
                raise ReflectionError('Uma animação ativa de coleta possui outro layout de player.')
            anim = gather_player_animation(schema,player)
            if anim is None:
                continue
            if schema.read(player['address']+813, 1)[0] & 1:
                continue
            interface_vtable = struct.unpack('<Q', schema.read(player['address']+40, 8))[0]
            getter = struct.unpack('<Q', schema.read(interface_vtable+64, 8))[0]
            if schema.read(getter, 7) != SEQUENCE_GETTER_BYTES:
                raise ReflectionError('O getter de reprodução de sequência de coleta mudou.')
            status = struct.unpack('<I', schema.read(player['address']+40+708, 4))[0]
            if status == 1:
                playing.add((anim['index'], anim['serial']))
    success_key = (animations['Anim_CompleteSuccess']['index'], animations['Anim_CompleteSuccess']['serial'])
    fail_key = (animations['Anim_CompleteFail']['index'], animations['Anim_CompleteFail']['serial'])
    return success_key in playing, fail_key in playing


def recover_gather_progress_code(process, base):
    definitions = json.loads((Path(base)/'activity_code_templates.json').read_text('utf-8'))
    return recover_named_anchors(process, definitions, REQUIRED_GATHER_PROGRESS_ANCHORS)


def observe_gather_progress(process, roots, base, visibility_layout, definitions, code_anchors=None):
    base = Path(base)
    definitions_code = json.loads((base/'activity_code_templates.json').read_text('utf-8'))
    if code_anchors is None:code_anchors = recover_gather_progress_code(process, base)
    anchors = [{'name':name,'rva':rva,'current':raw.hex(),'matches':1} for name,rva,raw in code_anchors]
    anchors.append({'session':process.session})
    checked_code = validate_captured_anchors(process, anchors, definitions_code, REQUIRED_GATHER_PROGRESS_ANCHORS)
    player = observe_player(process, roots)
    if not player['player_present']:
        verify_code_anchors(lambda rva,size:process.read(process.base+rva,size), checked_code)
        return GatherProgressFrame(session=process.session, stage='player_absent')
    registry = ObjectRegistry(process, roots)
    schema = ReflectionSchema(registry, max_bytes=1048576)
    registry.allow_widgets(player['roles']['player']['class_address'])
    game = registry.object(player['roles']['game_instance']['address'])
    hud = hud_roots(registry, schema, game)
    if not hud:
        schema.verify()
        registry.verify()
        verify_code_anchors(lambda rva,size:process.read(process.base+rva,size), checked_code)
        return GatherProgressFrame(session=process.session, stage='hud_absent')
    if hud[0]['class_name']!='HUD_Layer_C' or hud[0]['class_size'] not in HUD_GATHER_PROGRESS_LAYOUTS:
        details={'hud_root':hud[0]}
        if hud[0]['class_name']=='HUD_Layer_C':
            field=schema.field(hud[0],'HUD_Layer_C','GatherProgress')
            details['gather_progress_field']={'name':field.name,'kind':field.kind,
                'offset':field.offset,'stride':field.stride,'count':field.count}
        raise ReflectionError('O HUD de coleta possui outro layout nativo.',details=details)
    widget = schema.linked(hud[0], 'HUD_Layer_C', 'GatherProgress', HUD_GATHER_PROGRESS_LAYOUTS[hud[0]['class_size']])
    if widget is None:
        schema.verify()
        registry.verify()
        verify_code_anchors(lambda rva,size:process.read(process.base+rva,size), checked_code)
        return GatherProgressFrame(session=process.session, stage='progress_absent')
    if 'AionWidget_HUD_GatherProgress' not in {item['name'] for item in registry.ancestry(widget)}:
        raise ReflectionError('O widget de progresso de coleta possui outra hierarquia.')
    visibility = widget_visibility(schema, widget, visibility_layout, definitions)
    if not visibility['locally_visible']:
        schema.verify()
        registry.verify()
        verify_code_anchors(lambda rva,size:process.read(process.base+rva,size), checked_code)
        return GatherProgressFrame(session=process.session, stage='progress_hidden')
    source_id = struct.unpack('<I', schema.read(widget['address']+968, 4))[0]
    raw_final = schema.read(widget['address']+989, 1)[0]
    if raw_final not in (0, 1):
        raise ReflectionError('A flag de resultado de coleta não é booleana.')
    finalizing = bool(raw_final)
    success, failure = read_gather_result_animations(schema, widget)
    frame = GatherProgressFrame(
        session=process.session,
        stage='progress_visible',
        source_id=source_id,
        finalizing=finalizing,
        success_animation_playing=success,
        failure_animation_playing=failure,
    )
    frame.result_kind()
    schema.verify()
    registry.verify()
    verify_code_anchors(lambda rva,size:process.read(process.base+rva,size), checked_code)
    return frame


def active_interactions(actions):
    result = []
    for action in actions:
        started = action.get('client_started') if isinstance(action, dict) else None
        ended = action.get('client_ended') if isinstance(action, dict) else None
        if type(started) is not bool or type(ended) is not bool:
            raise ReflectionError('O ciclo de vida da interação local está indisponível.')
        if started and not ended:
            if not action.get('target'):
                raise ReflectionError('Interação ativa sem identidade de alvo.')
            result.append(action)
    if len({action.get('target') for action in result}) > 1:
        raise ReflectionError('O personagem possui interações ativas conflitantes.')
    return tuple(result)


class GatherResultTracker:
    def __init__(self, source_id, key):
        if type(source_id) is not int or not 0 < source_id < 4294967296 or not key:
            raise ReflectionError('Identidade de recurso ou alvo inválida para rastrear coleta.')
        self.source_id = source_id
        self.key = key
        self.started = False
        self.finished = False
        self.action_identity = None
        self.session = None

    def observe(self, actions, progress):
        if self.finished:
            raise ReflectionError('O resultado da coleta já foi consumido.')
        if not isinstance(progress, GatherProgressFrame):
            raise ReflectionError('É necessário um quadro nativo atual de progresso de coleta.')
        if not progress.session or self.session is not None and progress.session != self.session:
            raise ReflectionError('A sessão do progresso de coleta mudou.')
        self.session = progress.session
        interactions = active_interactions(actions)
        if any(action.get('target') != self.key for action in interactions):
            raise ReflectionError('Outro alvo possui a interação local atual.')
        for action in interactions:
            identity = (action.get('object_index'), action.get('serial_number'))
            if (any(type(value) is not int for value in identity)
                    or identity[0] < 0 or not 0 < identity[1] <= 0xffffffff):
                raise ReflectionError('Identidade nativa da interação indisponível.')
            if self.action_identity is None:
                self.action_identity = identity
            elif self.action_identity != identity:
                raise ReflectionError('Outra encarnação de interação substituiu a ação de coleta.')
        result = progress.result_kind()
        if progress.stage != 'progress_visible':
            if self.started:
                raise ReflectionError('O progresso de coleta desapareceu antes do resultado ser observado.')
            return None
        if progress.source_id != self.source_id:
            raise ReflectionError('O HUD de coleta pertence a outra definição de recurso.')
        if not self.started:
            if self.action_identity is None:
                raise ReflectionError('Progresso visível sem interação atribuída ao recurso.')
            if (progress.finalizing or progress.success_animation_playing
                    or progress.failure_animation_playing):
                raise ReflectionError('O novo progresso de coleta ainda exibe uma animação de resultado anterior.')
            self.started = True
            return None
        if result in ('success', 'failure'):
            self.finished = True
            return result
        return None
