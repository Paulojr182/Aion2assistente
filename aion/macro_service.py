"""Background worker for the focused macro feature; UI remains responsive."""
import threading,time
from collections import deque
from .macro_loop import MacroLoop,SnapshotUnavailable
from .activities import ActivityPolicy
from .activity_adapter import ActivityAdapter,LiveActivityDispatcher,LiveActivityObserver
from .native_player import RegistryChanged
from .name_pool import NamePoolChanged
from pathlib import Path
import json


def parse_combo(text):
    """'1:0.8, 3:1.2, 2' -> ((1,0.8),(3,1.2),(2,1.0)). Slots 1..8; espera em segundos (padrao 1.0)."""
    combo = []
    for part in str(text or '').replace(';', ',').split(','):
        part = part.strip()
        if not part: continue
        slot, _, wait = part.partition(':')
        try:
            s = int(slot.strip()); w = float(wait.strip()) if wait.strip() else 1.0
        except ValueError:
            continue
        if 1 <= s <= 8: combo.append((s, max(0.1, min(30.0, w))))
    return tuple(combo)


def _parse_options():
    try:
        p = Path(__file__).resolve().parent.parent / 'data' / 'profile.json'
        if p.is_file():
            opts = json.loads(p.read_text(encoding='utf-8')).get('options', {})
            center = None
            if opts.get('combat_positions'):
                try:
                    center = tuple(float(x.strip()) for x in opts['combat_positions'].split(','))
                except Exception:
                    center = None
            radius = None
            if opts.get('circle_radius_m'):
                try:
                    radius = float(opts['circle_radius_m']) * 100.0
                except Exception:
                    radius = None
            combat_names = [x.strip() for x in opts.get('combat_targets', '').split(',') if x.strip()]
            combo = parse_combo(opts.get('combat_combo', '')) if opts.get('combat_style') == 'combo' else ()
            gather_names = [x.strip() for x in opts.get('gather_targets', '').split(',') if x.strip()]
            waypoints = []
            for part in opts.get('gather_waypoints', '').split(';'):
                try:
                    pt = tuple(float(v) for v in part.split(','))
                    if len(pt) == 3: waypoints.append(pt)
                except ValueError:
                    pass
            try:
                stamina = max(0.0, min(1.0, float(opts.get('flight_min_stamina_pct', 70)) / 100.0))
            except Exception:
                stamina = 0.70
            return {'center': center, 'radius': radius, 'flight_min_stamina': stamina, 'combo': combo,
                    'combat_names': combat_names, 'gather_names': gather_names, 'waypoints': waypoints,
                    'ground_only': bool(opts.get('gather_ground_only')),
                    'combat_exclude': bool(opts.get('combat_exclude_targets')),
                    'gather_exclude': bool(opts.get('gather_exclude_targets'))}
    except Exception:
        pass
    return {'center': None, 'radius': None, 'flight_min_stamina': 0.70, 'combo': (), 'combat_names': [], 'gather_names': [],
            'combat_exclude': False, 'gather_exclude': False}


class MacroService:
    def __init__(self,factory,*,interval=0.12):
        self.factory=factory;self.interval=interval
        self.desired=threading.Event();self.closing=threading.Event()
        self.lock=threading.Lock();self.thread=None;self.mode='combat';self.request_generation=0
        self.backend=None
        self.status={'connected':False,'running':False,'routine':'combat','state':'initializing','message':'Validando a conexão com o jogo…'}

    def update(self,**values):
        with self.lock:self.status.update(values)

    def get_status(self):
        with self.lock:
            return {**self.status,'requested_running':self.desired.is_set(),'requested_routine':self.mode}

    def scan_targets(self):
        backend = self.backend
        if backend is None or not hasattr(backend, 'reader'):
            return {'monsters': [], 'gatherables': []}
        reader = backend.reader
        try:
            import importlib
            import aion.map_nav_probe
            importlib.reload(aion.map_nav_probe)
            aion.map_nav_probe.probe_nav(reader)
        except Exception:
            pass
        opts = _parse_options()
        radius = opts.get('radius') or 10000.0

        gather_radius = max(radius, 20000.0)
        gatherables = []
        try:
            from .friendly_names import resolve_gather_name
            report = reader.gather_sources()
            for t in report.get('targets', ()):
                raw_name = t.get('source', {}).get('name')
                dist = t.get('distance_engine_units', 0.0)
                if raw_name and dist <= gather_radius:
                    disp_name = resolve_gather_name(raw_name)
                    if disp_name not in gatherables:
                        gatherables.append(disp_name)
        except Exception:
            pass

        monsters = []
        try:
            from .friendly_names import resolve_monster_name
            with reader.process(33554432) as process:
                from .native_player import observe_player, ObjectRegistry
                from .reflection_fields import ReflectionSchema
                from .native_positions import actor_position
                from .native_scene import observe_scene
                from .native_npc import read_cached_npc
                import math
                player = observe_player(process, reader.roots)
                if player.get('player_present'):
                    registry = ObjectRegistry(process, reader.roots)
                    pawn = registry.object(player['roles']['player']['address'])
                    schema = ReflectionSchema(registry, max_bytes=524288)
                    pos_info = actor_position(registry, schema, pawn)
                    p_pos = pos_info.get('position')
                    scene = observe_scene(process, reader.roots, report_limit=512, known_keys_only=True, class_filter='BPCh_NpcBase_C')
                    seen = set()
                    mob_radius = max(radius, 15000.0)
                    for collection in scene.get('collections', ()):
                        for entry in collection.get('entries', ()):
                            identity = (entry['index'], entry['serial'])
                            if identity in seen:
                                continue
                            seen.add(identity)
                            try:
                                actor = registry.resolve_pair(*identity)
                                if actor is None:
                                    continue
                                actor_pos = actor_position(registry, schema, actor)
                                a_pos = actor_pos.get('position')
                                if p_pos and a_pos:
                                    d = math.hypot(p_pos[0] - a_pos[0], p_pos[1] - a_pos[1])
                                    if d > mob_radius:
                                        continue
                                defn = read_cached_npc(registry, schema, actor, reader.npc_layout, reader.npc_enums)
                                if defn.get('base_definition_attackable'):
                                    internal = defn.get('internal_name', {}).get('base', '')
                                    res = defn.get('resource_name', {}).get('base', '')
                                    lvl = defn.get('level')
                                    m_label = resolve_monster_name(internal, res, lvl)
                                    if m_label and m_label not in monsters:
                                        monsters.append(m_label)
                            except Exception:
                                pass
        except Exception:
            pass
        return {'monsters': monsters, 'gatherables': gatherables}


    # --- farm de coleta (chao + alto) usado pelo painel -------------------------------------
    def _farm_alive(self):
        t = getattr(self, '_farm_thread', None)
        return t is not None and t.is_alive()

    def _farm_tick(self, backend, active, position):
        with self.lock: generation = self.request_generation
        if active and not self._farm_alive():
            self._farm_start(backend, generation)
        message = getattr(self, '_farm_message', '') or 'Iniciando a coleta…'
        running = active and self._farm_alive()
        stats = getattr(getattr(self, '_farm_routine', None), 'stats', {}) or {}
        if not active and not self._farm_alive():
            message = 'Coleta parada.'
        self.update(connected=True, running=running, routine='gathering',
                    state='gathering' if running else ('stopping' if self._farm_alive() else 'stopped'),
                    message=message, player_position=position, macro_active=False, farm_stats=dict(stats))

    def _farm_start(self, backend, generation):
        from .route_nav import RouteNavigator
        from .gather_routine import GatherRoutine
        opts = _parse_options()
        def log(msg):
            self._farm_message = msg
        def stop():
            return (self.closing.is_set() or not self.desired.is_set()
                    or self.mode != 'gathering' or self.request_generation != generation)
        def run():
            nav = None
            try:
                ground_only = bool(opts.get('ground_only'))
                nav = RouteNavigator(backend, backend.reader, log=log, flight=not ground_only)
                routine = GatherRoutine(nav, backend, backend.reader, log=log, stop=stop)
                routine.min_stamina = opts.get('flight_min_stamina', 0.70)
                routine.names = set(opts['gather_names']) if opts['gather_names'] and not opts['gather_exclude'] else None
                def refresh_names():
                    o = _parse_options()
                    return set(o['gather_names']) if o['gather_names'] and not o['gather_exclude'] else None
                routine.refresh_names = refresh_names
                routine.waypoints = list(opts.get('waypoints') or [])
                routine.ground_only = ground_only
                self._farm_routine = routine
                routine.run(radius=opts['radius'], center=opts['center'])
                if not stop(): log('Coleta encerrada.')
            except Exception as exc:
                log(f'Coleta interrompida: {exc}')
            finally:
                if nav is not None:
                    for fn in (nav.release_all, lambda: nav.set_running(False)):
                        try: fn()
                        except Exception: pass
                try: backend.stop_macro(force=True)
                except Exception: pass
                if not stop():
                    with self.lock:
                        if self.request_generation == generation: self.desired.clear()
        self._farm_message = 'Iniciando a coleta…'
        self._farm_thread = threading.Thread(target=run, name='AionGatherFarm', daemon=True)
        self._farm_thread.start()

    def launch(self):
        if self.thread is not None:raise RuntimeError('Controlador já iniciado.')
        self.thread=threading.Thread(target=self._run,name='AionAutoMacro',daemon=True);self.thread.start()

    def start(self,mode='combat'):
        if mode not in ('combat','gathering','quests'):
            raise ValueError('Rotina desconhecida.')
        if self.get_status()['state']=='error':raise RuntimeError('Reconecte o jogo antes de iniciar.')
        with self.lock:
            self.mode=mode;self.request_generation+=1
            self.desired.set()

    def stop(self):
        with self.lock:
            self.desired.clear();self.request_generation+=1
            if self.status['state']!='error':
                self.status['message']='Encerrando a rotina…'

    def _run(self):
        backend=None;loop=None
        try:
            for attempt in range(5):
                if self.closing.is_set():
                    return
                try:
                    backend=self.factory(lambda text:self.update(message=text))
                    self.backend=backend
                    try:
                        import aion.map_nav_probe
                        aion.map_nav_probe.probe_nav(backend.reader)
                    except Exception:
                        pass
                    break
                except (RegistryChanged,NamePoolChanged):
                    if attempt==4:
                        raise
                    self.update(message='O jogo está atualizando os objetos. Tentando conectar novamente…')
                    if self.closing.wait(0.25):
                        return
            opts=_parse_options()
            loop=MacroLoop(backend,names=opts['combat_names'],center=opts['center'],radius=opts['radius'])
            activity_adapter=None;activity_mode=None;activity_generation=None;activity_message=None;last_preview=-1.0;unstable=0;defeated=deque(maxlen=32);defeated_total=0;attacked={};damage_events=0;last_scope=None;reported_deaths=set();collect_at=None;pending_collections=0
            autodefending=False;last_gather_health=None;current_position=None;last_pos_poll=-1.0;observed_monsters=set()
            self.update(connected=True,state='stopped',message='Pronto para iniciar.')
            while not self.closing.is_set():
                active=self.desired.is_set()
                with self.lock:current_mode=self.mode
                now_mono = time.monotonic()
                if now_mono - last_pos_poll >= 0.5 and hasattr(backend, 'reader'):
                    last_pos_poll = now_mono
                    try:
                        st = backend.reader.flight_state()
                        pos = st.get('position', {}).get('position')
                        if pos:
                            current_position = [round(pos[0], 1), round(pos[1], 1), round(pos[2], 1)]
                    except Exception:
                        pass
                if current_mode == 'gathering' and hasattr(backend, 'reader') and (active or self._farm_alive()):
                    self._farm_tick(backend, active, current_position)
                    self.closing.wait(0.25)
                    continue
                if active or loop.was_running or activity_adapter is not None or now_mono-last_preview>=1:
                    try:
                        snapshot=backend.snapshot();unstable=0
                    except SnapshotUnavailable:
                        backend.stop_macro(force=False);unstable+=1
                        self.update(state='waiting',running=self.desired.is_set(),message='Aguardando a atualização do alvo.',macro_active=None)
                        self.closing.wait(min(1.0,0.15*unstable));continue
                    last_preview=time.monotonic()
                    if snapshot.target and snapshot.target.label:
                        clean = snapshot.target.label.split(' nível')[0].strip()
                        if clean and clean not in ('Inimigo', 'alvo', 'boss'):
                            observed_monsters.add(clean)
                    # Stop requested during a read must win over the old value.
                    active=self.desired.is_set() and not self.closing.is_set()
                    with self.lock:current_mode=self.mode;current_generation=self.request_generation
                    if active and current_mode in ('gathering','quests'):
                        if last_gather_health is not None and snapshot.player_health < last_gather_health:
                            if snapshot.target is None or not snapshot.target.eligible:
                                try:
                                    backend.next_target(snapshot)
                                except Exception:
                                    pass
                        last_gather_health = snapshot.player_health
                        has_mob = (snapshot.target is not None and snapshot.target.eligible and snapshot.target.health > 0)
                        if has_mob:
                            autodefending = True
                            if not snapshot.macro_active:
                                try:
                                    backend.ensure_macro(snapshot)
                                except Exception:
                                    pass
                            state = {'state': 'autodefense',
                                     'message': f'Autodefesa: combatendo {snapshot.target.label} — HP {snapshot.target.health:,}/{snapshot.target.maximum:,}'.replace(',','.'),
                                     'target_label': snapshot.target.label, 'macro_active': True}
                            self.update(**state, connected=True, running=True, routine=current_mode,
                                        player_health=snapshot.player_health, player_maximum=snapshot.player_maximum,
                                        player_position=current_position, nearby_monsters=sorted(observed_monsters),
                                        telemetry=getattr(backend,'telemetry',lambda:{})())
                            self.closing.wait(self.interval)
                            continue
                        elif autodefending:
                            backend.stop_macro(force=True)
                            autodefending = False
                            collect_fn = getattr(backend, 'collect', None)
                            if collect_fn is not None:
                                try:
                                    collect_fn(snapshot)
                                except Exception:
                                    pass
                        backend.stop_macro(force=True)
                        loop.was_running=False;loop.target=None;collect_at=None;pending_collections=0
                        if (activity_adapter is None or activity_mode!=current_mode
                                or activity_generation!=current_generation):
                            if activity_adapter is not None:activity_adapter.policy.stop()
                            activity_message=None
                            curr_opts = _parse_options()
                            policy=ActivityPolicy(current_mode, max_age=5.0, timeout=180.0, min_navigation_progress=1.0,
                                                  names=curr_opts['gather_names'],
                                                  exclude=curr_opts['gather_exclude'],
                                                  center=curr_opts['center'], radius=curr_opts['radius'])
                            policy.start()
                            confirmation='hud' if current_mode=='gathering' else 'inventory'
                            observer=LiveActivityObserver(backend.reader,confirmation=confirmation,flight_enabled=current_mode=='gathering',allow_passive_target=True) if hasattr(backend,'reader') else (lambda now:None)
                            dispatcher=LiveActivityDispatcher(backend,flight_enabled=True,duration_ms=1200,chain_pulses=1,max_age=60.0,allow_passive_target=True,
                                cancelled=lambda: not self.desired.is_set() or self.closing.is_set()
                                    or self.mode!=activity_mode or self.request_generation!=activity_generation)
                            activity_adapter=ActivityAdapter(policy,observer,dispatcher,combat_active=lambda:False,
                                cancelled=lambda: not self.desired.is_set() or self.closing.is_set()
                                    or self.mode!=activity_mode or self.request_generation!=activity_generation,
                                clock=time.monotonic,confirmation=confirmation)
                            activity_mode=current_mode
                            activity_generation=current_generation
                        act_cmd = None
                        try:
                            act_cmd = activity_adapter.tick(time.monotonic())
                            state={'state':current_mode,'message':activity_adapter.policy.status,
                                   'collection_confirmation':activity_adapter.confirmation,
                                   'last_collection_result':activity_adapter.last_collection_result,
                                   'item_receipt_confirmed':False if activity_adapter.confirmation=='hud' else None}
                        except Exception as exc:
                            activity_adapter.policy.stop()
                            state={'state':'stopped','message':f'Rotina interrompida: {exc}'}
                        if not activity_adapter.policy.running:
                            with self.lock:
                                if self.request_generation==current_generation:
                                    self.desired.clear();active=False
                                    state['state']='stopped';activity_message=state['message']
                                else:
                                    active=self.desired.is_set() and not self.closing.is_set()
                                    state={'state':'waiting' if active else 'stopped',
                                        'message':'Revalidando nova solicitação.' if active else 'Rotina parada.'}
                        self.update(**state,connected=True,running=active,routine=current_mode,macro_active=False,
                            player_health=snapshot.player_health,player_maximum=snapshot.player_maximum,
                            player_position=current_position, nearby_monsters=sorted(observed_monsters),
                            target_label=snapshot.target.label if snapshot.target else '',
                            telemetry=getattr(backend,'telemetry',lambda:{})())
                        step_wait = 0.01 if (act_cmd and getattr(act_cmd, 'operation', None) == 'navigate') else self.interval
                        self.closing.wait(step_wait)
                        continue
                    if not active and current_mode in ('gathering','quests'):
                        if activity_adapter is not None:
                            activity_adapter.policy.stop()
                            activity_adapter=None;activity_mode=None
                        self.update(connected=True,running=False,state='stopped',routine=current_mode,
                            message=activity_message or 'Rotina parada.',macro_active=snapshot.macro_active,
                            player_health=snapshot.player_health,player_maximum=snapshot.player_maximum,
                            target_label=snapshot.target.label if snapshot.target else '')
                        self.closing.wait(0.2)
                        continue
                    if activity_adapter is not None:
                        activity_adapter.policy.stop()
                        activity_adapter=None;activity_mode=None
                    if last_scope!=snapshot.scope:
                        last_scope=snapshot.scope
                        attacked.clear()
                        reported_deaths.clear()
                        collect_at=None
                        pending_collections=0
                    previously_running=loop.was_running
                    now=time.monotonic()
                    if active and snapshot.player_health>0:
                        deaths=set(snapshot.defeated)
                        if snapshot.target is not None and snapshot.target.eligible and snapshot.target.health<=0:
                            deaths.add(snapshot.target.identity)
                        for identity in deaths.intersection(attacked):
                            damage_events+=1
                            attacked.pop(identity)
                            defeated.append(identity)
                            defeated_total+=1
                            reported_deaths.add(identity)
                            pending_collections+=1
                            if collect_at is None:
                                collect_at=now+0.35
                    else:
                        collect_at=None
                        pending_collections=0
                        if previously_running:
                            attacked.clear()
                    if collect_at is not None:
                        backend.stop_macro(force=True)
                        if now>=collect_at and self.desired.is_set() and not self.closing.is_set():
                            collect=getattr(backend,'collect',None)
                            if collect is not None:
                                try:
                                    collect(snapshot)
                                except (RegistryChanged,NamePoolChanged):
                                    self.closing.wait(0.15)
                                    continue
                                except OSError:
                                    state={'state':'collecting','message':'O jogo recusou o envio de F.'}
                                else:
                                    state={'state':'collecting','message':'Comando de coleta enviado ao jogo.'}
                            else:
                                state={'state':'collecting','message':'Alvo derrotado.'}
                            pending_collections-=1
                            collect_at=now+0.35 if pending_collections else None
                        else:
                            state={'state':'collecting','message':'Aguardando os itens do inimigo.'}
                    else:
                        try:
                            c_opts = _parse_options()
                            loop.names = frozenset(c_opts['combat_names'])
                            loop.center = c_opts['center']
                            loop.radius = c_opts['radius']
                            if loop.combo != c_opts['combo']:
                                loop.combo = c_opts['combo']; loop.combo_index = 0
                            state=loop.step(snapshot,running=active,now=now)
                        except (SnapshotUnavailable,RegistryChanged,NamePoolChanged):
                            backend.stop_macro(force=False)
                            self.update(state='waiting',running=active,macro_active=None,
                                        message='Aguardando uma leitura atualizada do jogo.')
                            self.closing.wait(0.15)
                            continue
                        if active and state['state']=='attacking' and snapshot.target is not None:
                            identity=snapshot.target.identity
                            if identity not in reported_deaths:
                                previous_health=attacked.get(identity)
                                if previous_health is not None and snapshot.target.health<previous_health:
                                    damage_events+=1
                                attacked[identity]=snapshot.target.health
                                while len(attacked)>128:
                                    attacked.pop(next(iter(attacked)))
                    self.update(**state,connected=True,running=active,routine='combat',
                        macro_active=state['state']=='attacking' if active else (False if previously_running else snapshot.macro_active),
                        player_health=snapshot.player_health,player_maximum=snapshot.player_maximum,
                        player_position=current_position,nearby_monsters=sorted(observed_monsters),
                        target_label=snapshot.target.label if snapshot.target else '',
                        selected_target=list(snapshot.target.identity) if snapshot.target else None,
                        selected_target_health=snapshot.target.health if snapshot.target else None,
                        selected_target_maximum=snapshot.target.maximum if snapshot.target else None,
                        selected_target_eligible=snapshot.target.eligible if snapshot.target else False,
                        defeated_targets=[list(identity) for identity in defeated],defeated_total=defeated_total,
                        damage_events=damage_events,
                        telemetry=getattr(backend,'telemetry',lambda:{})())
                self.closing.wait(self.interval if active else 0.2)
        except Exception as exc:
            self.desired.clear()
            self.update(connected=False,running=False,state='error',macro_active=None,message='A conexão foi interrompida. Reconecte ao jogo.',detail=str(exc))
        finally:
            stop_error=None
            try:
                if loop is not None:loop.close()
            except Exception as exc:
                stop_error=exc
            try:
                if backend is not None:backend.close()
            except Exception as exc:stop_error=stop_error or exc
            if stop_error is not None:
                self.update(state='error',running=False,macro_active=None,message='Não foi possível confirmar a parada. Verifique o jogo.',detail=str(stop_error))
            elif self.closing.is_set() and self.get_status()['state']!='error':
                self.update(running=False,state='stopped',macro_active=False,message='Ataque automático parado.')

    def shutdown(self,timeout=4):
        self.desired.clear();self.closing.set()
        if self.thread is not None:self.thread.join(timeout)
        return self.thread is None or not self.thread.is_alive()
