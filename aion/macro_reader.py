"""Current native observations used by the focused auto-macro controller."""
from contextlib import contextmanager
from pathlib import Path
import json,struct,time
from .name_pool import NamePoolChanged
from .native_memory import ReadOnlyProcess
from .metadata_templates import InstructionTemplate
from .native_metadata import recover_observation
from .native_player import observe_player,ObjectRegistry,PlayerObservationError,RegistryChanged
from .reflection_fields import ReflectionSchema,ReflectionError,ReflectionChanged
from .resource_container import ResourceContainerError
from .resource_layout import recover_resource_layout,validate_resource_code
from .native_resources import actor_resources
from .native_target import observe_target
from .scene_objects import SceneReadError,SceneChanged
from .dialog_map import DialogMapError
from .target_code import recover_visibility, widget_visibility
from .npc_layout import recover_npc_layout,validate_npc_layout
from .native_npc_schema import observe_npc_schema
from .native_npc import read_cached_npc
from .action_bindings import observe_action_bindings
from .native_movement import observe_movement_profile
from .native_flight import observe_flight_state
from .native_gather_sources import observe_gather_sources, recover_gather_source_profile
from .native_gather_progress import observe_gather_progress, recover_gather_progress_code
from .native_inventory import observe_inventory_with_bag
from .native_interactions import observe_interactions, recover_interaction_code
from .native_contextual import observe_contextual, recover_contextual_code
from .native_bag import observe_bag_state, recover_bag_code
from .code_anchors import recover_code_anchors
from .native_quests import observe_quests, recover_quest_code
from .macro_loop import MacroSnapshot,MacroTarget,SnapshotUnavailable,TargetChanged


class MacroReader:
    def __init__(self,base,pid,window,progress=lambda message:None):
        self.base=Path(base);self.pid=pid;self.window=window;self.session=None
        self.recent_enemies={}
        self.confirmed_defeated={}
        self.inventory_anchors=None
        self.quest_anchors=None
        self.gather_progress_anchors=None
        self.gather_source_profile=None
        self.interaction_anchors=None
        self.contextual_anchors=None
        self.bag_anchors=None
        from .game_image import image_path
        self.image=image_path(pid)
        self.load=lambda name:json.loads((self.base/name).read_text('utf-8'))
        progress('Validando a conexão com o jogo…')
        with self.process(67108864) as process:
            records=self.load('metadata_templates.json')
            templates=tuple(InstructionTemplate(item['name'],item['old_rva'],bytes.fromhex(item['expected']),
                tuple(tuple(pair) for pair in item['operands']),item['radius']) for item in records)
            self.roots,_=recover_observation(process,templates,wide=True)
            if self.roots is None:raise RuntimeError('Esta versão do jogo não foi reconhecida.')
            self.resource_definitions=self.load('resource_templates.json')
            self.resources=recover_resource_layout(process,self.resource_definitions)
            if not self.resources.get('read_layout_validated'):raise RuntimeError('Leitura de HP não validada.')
            progress('Validando inimigos e habilidades do macro…')
            self.target_definitions=self.load('target_templates.json')
            self.visibility=recover_visibility(process,self.target_definitions)
            self.npc_definition=self.load('npc_layout.json')
            self.npc_layout=recover_npc_layout(process,self.npc_definition)
            self.session=process.session
        # NPC declaration fallback has its own independent bounded search.
        with self.process(67108864) as process:
            declaration=observe_npc_schema(process,self.roots,self.load('npc_schema.json'))
            if not declaration.get('npc_schema_validated'):raise RuntimeError('Definições de inimigos não validadas.')
            self.npc_enums=declaration['enums']
            self.action_definitions=self.load('internal_action_templates.json')
            self.state_profile=self.load('macro_state_templates.json')
            self.validate_macro_code(process)
            self.session=process.session

    @contextmanager
    def process(self,budget=8388608):
        with ReadOnlyProcess(self.pid,str(self.image),max_bytes=budget) as process:
            if self.session is not None and process.session!=self.session:
                raise RuntimeError('A sessão do jogo mudou. Reconecte o assistente.')
            yield process

    def validate_macro_code(self,process):
        profile=self.state_profile
        if profile['image_size']!=process.image_size or profile['active_offset']!=1496 or profile['player_action_system_offset']!=64:
            raise RuntimeError('Layout do macro não reconhecido.')
        if {item['name'] for item in profile['templates']}!={'macro_start_state','macro_stop_state'}:
            raise RuntimeError('Perfil do macro incompleto.')
        for item in profile['templates']:
            template=InstructionTemplate(item['name'],item['old_rva'],bytes.fromhex(item['expected']),
                                         tuple(tuple(pair) for pair in item['operands']))
            rva=item['old_rva'];raw=process.read(process.base+rva,len(template.expected))
            if not template.pattern().fullmatch(raw):raise RuntimeError('O código do macro mudou nesta versão do jogo.')
            for offset,end in template.operands:
                target=rva+end+struct.unpack_from('<i',raw,offset)[0]
                if not 0<=target<process.image_size:raise RuntimeError('Código de macro fora da imagem validada.')

    def bindings(self,action='Macro',number=0):
        with self.process() as process:
            result=observe_action_bindings(process,self.roots,self.action_definitions,action_name=action,action_number=number)
            if not result.get('player_present'):raise RuntimeError('Entre no personagem para iniciar.')
            return result

    def movement_profile(self):
        with self.process() as process:
            return observe_movement_profile(process,self.roots)

    def flight_state(self, *, with_auto_state=False):
        # Each attempt opens a fresh observation and still validates registry identity.
        # Object churn during takeoff is not permission to reuse a stale observation.
        for attempt in range(3):
            try:
                with self.process() as process:
                    result = observe_flight_state(process,self.roots,with_position=True)
                    if with_auto_state:
                        player = observe_player(process,self.roots)['roles']['player']
                        if (player['index'],player['serial']) != tuple(result['player_identity']):
                            raise RegistryChanged('Personagem mudou durante leitura da transicao de voo.')
                        result = dict(result)
                        result['auto_move_state'] = process.read(player['address']+0x1760,1)[0]
                    return result
            except (RegistryChanged,ReflectionChanged):
                if attempt == 2:
                    raise
                time.sleep(0.02)

    def flight_stamina_ratio(self):
        from .resource_container import capture_resources
        try:
            with self.process(8388608) as process:
                player=observe_player(process,self.roots)
                if not player.get('player_present'):return None
                registry=ObjectRegistry(process,self.roots)
                role=player['roles']['player']
                actor=registry.resolve_pair(role['index'],role['serial'])
                if actor is None:return None
                layout=self.resources
                address=actor['address']+layout['private_offset']
                raw=process.read(address,8)
                private=struct.unpack('<Q',raw)[0]
                if not private:return None
                values=capture_resources(process.read,private+layout['container_offset'],max_slots=512,max_bytes=65536)
                for cur_k,max_k in ((3,11),(4,12),(5,13),(6,14)):
                    if cur_k in values and max_k in values:
                        max_v=values[max_k]&0xFFFFFFFF
                        if max_v>0:
                            cur_v=values[cur_k]&0xFFFFFFFF
                            return max(0.0,min(1.0,cur_v/float(max_v)))
                return 1.0
        except Exception:
            return 1.0

    def gather_sources(self):
        with self.process(33554432) as process:
            if self.gather_source_profile is None:
                self.gather_source_profile=recover_gather_source_profile(process,self.base)
            for attempt in range(2):
                try:return observe_gather_sources(process,self.roots,self.base,self.gather_source_profile)
                except (RegistryChanged,NamePoolChanged,SceneChanged):
                    if attempt:raise

    def gather_progress(self):
        with self.process(33554432) as process:
            if self.gather_progress_anchors is None:
                self.gather_progress_anchors=recover_gather_progress_code(process,self.base)
            return observe_gather_progress(process,self.roots,self.base,self.visibility,self.target_definitions,
                self.gather_progress_anchors)

    def inventory(self):
        with self.process(33554432) as process:
            if self.inventory_anchors is None:
                definitions=self.load('inventory_code_templates.json')
                templates=tuple(InstructionTemplate(d['name'],d['old_rva'],bytes.fromhex(d['expected']),
                    tuple(tuple(pair) for pair in d['operands']),d['radius']) for d in definitions)
                self.inventory_anchors=recover_code_anchors(
                    lambda rva,size:process.read(process.base+rva,size),process.image_size,templates)
            if self.bag_anchors is None:self.bag_anchors=recover_bag_code(process,self.base)
            return observe_inventory_with_bag(process,self.roots,self.base,self.inventory_anchors,self.bag_anchors)

    def interactions(self):
        with self.process(16777216) as process:
            if self.interaction_anchors is None:
                self.interaction_anchors=recover_interaction_code(process,self.base)
            return observe_interactions(process,self.roots,self.base,self.interaction_anchors)

    def contextual(self):
        with self.process(16777216) as process:
            if self.contextual_anchors is None:
                self.contextual_anchors=recover_contextual_code(process,self.base)
            for attempt in range(2):
                try:return observe_contextual(process,self.roots,self.base,self.contextual_anchors,
                    self.visibility,self.target_definitions)
                except (ReflectionChanged,RegistryChanged,NamePoolChanged):
                    if attempt:raise

    def bag_state(self):
        with self.process(16777216) as process:
            if self.bag_anchors is None:self.bag_anchors=recover_bag_code(process,self.base)
            return observe_bag_state(process,self.roots,self.base,self.bag_anchors)

    def quests(self):
        with self.process(33554432) as process:
            if self.quest_anchors is None:self.quest_anchors=recover_quest_code(process,self.base)
            return observe_quests(process,self.roots,self.base,self.quest_anchors)

    def quest_target_marker(self, snapshot):
        """Read the interaction marker on the exact selected enemy's nameplate."""
        with self.process() as process:
            registry = ObjectRegistry(process, self.roots)
            player = observe_player(process, self.roots)
            if not player['player_present']:
                raise TargetChanged('Personagem indisponível para ler a marca de missão.')
            registry.allow_widgets(player['roles']['player']['class_address'])
            actor = registry.resolve_pair(*snapshot.target.identity)
            if actor is None:
                raise TargetChanged('O alvo mudou antes da leitura da marca de missão.')
            schema = ReflectionSchema(registry, max_bytes=524288)
            component = schema.linked(actor, 'AionCharacter', 'NameplateWidgetComponent')
            nameplate = schema.linked(component, 'AionLevelUIComponent', 'LevelUI') if component else None
            marker = (schema.linked(nameplate, 'AionWidget_NamePlateChar', 'P_Pan_InteractionMarker')
                      if nameplate else None)
            if marker is None:
                return None
            visible = widget_visibility(schema, marker, self.visibility, self.target_definitions)
            schema.verify()
            registry.verify()
            return visible['locally_visible'] if visible['slate_source'] != 'absent' else None

    def snapshot(self):
        for attempt in range(2):
            try:return self._snapshot_once()
            except (PlayerObservationError,ReflectionError,ResourceContainerError,NamePoolChanged,SceneReadError,DialogMapError) as exc:
                if attempt:raise SnapshotUnavailable('A leitura mudou durante a atualização do jogo.') from exc
                time.sleep(0.005)

    def _snapshot_once(self):
        started=time.monotonic()
        with self.process() as process:
            self.validate_macro_code(process)
            validated=validate_resource_code(lambda rva,size:process.read(process.base+rva,size),process.image_size,
                                             self.resources['source_rva'],self.resource_definitions)
            if any(self.resources.get(key)!=value for key,value in validated.items()):
                raise RuntimeError('O layout de HP mudou. Reconecte o assistente.')
            if validate_npc_layout(lambda rva,size:process.read(process.base+rva,size),process.image_size,
                                  self.npc_layout['code_rva'],self.npc_definition)!=self.npc_layout:
                raise RuntimeError('O layout de inimigos mudou. Reconecte o assistente.')
            player=observe_player(process,self.roots)
            if not player['player_present']:raise RuntimeError('Entre no personagem para iniciar.')
            registry=ObjectRegistry(process,self.roots);schema=ReflectionSchema(registry,max_bytes=1048576)
            actor=registry.object(player['roles']['player']['address'])
            session=registry.object(player['roles']['session']['address'])
            system=schema.linked(session,'AionCharSession','PlayerActionSystem')
            controller=schema.linked(actor,'Pawn','Controller')
            if system is None or system['class_name']!='PlayerActionSystem' or controller is None:
                raise RuntimeError('Aguardando o personagem estar pronto.')
            input_=schema.linked(controller,'AionPlayerController','GamePlayerInput')
            if input_ is None or input_['class_name']!='AionGamePlayerInput':raise RuntimeError('Entrada do personagem indisponível.')
            field=schema.field(session,'AionCharSession','PlayerActionSystem')
            if field.offset!=64 or system['class_size']<=1496:raise RuntimeError('Layout de habilidades mudou.')
            raw=schema.read(system['address']+1496,1)
            if raw not in (b'\x00',b'\x01'):raise RuntimeError('Estado do macro não confirmado.')
            health=actor_resources(registry,actor,self.resources).get('health')
            if health is None:raise RuntimeError('HP do personagem indisponível.')
            selected=observe_target(process,self.roots,self.visibility,self.target_definitions,self.resources)
            target=None
            if selected['selection_current_verified']:
                candidates=[item for item in selected['targets'] if item.get('selection_current_verified')]
                identities={(item['actor']['index'],item['actor']['serial']) for item in candidates}
                if len(identities)!=1:raise RuntimeError('Alvo selecionado ambíguo.')
                identity=next(iter(identities));npc=registry.resolve_pair(*identity)
                if npc is not None:
                    eligible=False;label='alvo'
                    owners=registry.ancestry(npc)
                    if any(owner['name']=='BPCh_NpcBase_C' for owner in owners):
                        definition=read_cached_npc(registry,schema,npc,self.npc_layout,self.npc_enums)
                        eligible=definition.get('base_definition_attackable') is True
                        from .friendly_names import resolve_monster_name
                        internal = definition.get('internal_name', {}).get('base', '')
                        res = definition.get('resource_name', {}).get('base', '')
                        lvl = definition.get('level')
                        friendly = resolve_monster_name(internal, res, lvl)
                        is_boss = any(item['role']=='boss' for item in candidates)
                        default_lbl = ('boss' if is_boss else 'inimigo') + f" nível {lvl if lvl is not None else '?'}"
                        label = friendly if friendly else default_lbl
                    values=(actor_resources(registry,npc,self.resources).get('health')
                            if any(owner['name']=='AionCharacter' for owner in owners) else None)
                    if values is not None:
                        target=MacroTarget(identity,int(values['current_decimal']),int(values['maximum_decimal']),eligible,label)
            schema.verify();registry.verify()
            scope=self.session+':'+':'.join(f"{obj['index']}/{obj['serial']}" for obj in (actor,session,input_,system))
            defeated=[]
            now=time.monotonic()
            if target is not None and target.eligible:
                self.recent_enemies[target.identity]=(scope,now)
            for identity,(old_scope,last_seen) in list(self.recent_enemies.items()):
                if old_scope!=scope or now-last_seen>2:
                    self.recent_enemies.pop(identity,None);continue
                if target is not None and identity==target.identity:
                    if target.health<=0:
                        defeated.append(identity);self.recent_enemies.pop(identity,None)
                    continue
                # A dead target's HUD may disappear before the next UI sample.
                # Verify its health by the same weak identity, never use it to attack.
                try:
                    history=ObjectRegistry(process,self.roots);old=history.resolve_pair(*identity)
                    if old is None:
                        self.recent_enemies.pop(identity,None);continue
                    old_health=actor_resources(history,old,self.resources).get('health');history.verify()
                    if old_health is not None and int(old_health['current_decimal'])==0:
                        defeated.append(identity);self.recent_enemies.pop(identity,None)
                except (OSError,ValueError,RuntimeError):pass
            while len(self.recent_enemies)>4:self.recent_enemies.pop(next(iter(self.recent_enemies)))
            for identity in defeated:self.confirmed_defeated[identity]=True
            while len(self.confirmed_defeated)>64:self.confirmed_defeated.pop(next(iter(self.confirmed_defeated)))
            return MacroSnapshot(scope,started,int(health['current_decimal']),int(health['maximum_decimal']),bool(raw[0]),target,
                                 tuple(self.confirmed_defeated),(actor['index'],actor['serial']),(input_['index'],input_['serial']))

    def require_target(self,expected):
        current=self.snapshot()
        if (current.scope!=expected.scope or current.player_health<=0 or current.target is None
            or expected.target is None or current.target.identity!=expected.target.identity
            or not current.target.eligible or current.target.health<=0):
            raise TargetChanged('O alvo mudou antes da ativação do macro.')
        return current
