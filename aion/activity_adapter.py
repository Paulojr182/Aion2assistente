"""Adapter connecting ActivityPolicy to verified native observations and commands."""
from dataclasses import dataclass, replace
import math
import time
from .activities import ActivityObservation, ActivityPolicy, ActivityTarget, Quest
from .macro_loop import SnapshotUnavailable
from .gather_receipt import GatherEvidence, GatherOutcome, GatherReceipt
from .gather_identity import format_gather_identity
from .gather_evidence import GatherSourceHistory,build_gather_evidence
from .native_gather_progress import active_interactions
from .gather_hud_attempt import GatherHudAttempt
from .navigation3d import approach_3d, requires_flight
from .native_player import RegistryChanged
from .name_pool import NamePoolChanged


from .friendly_names import resolve_gather_name


def planar_direction(origin, destination, *, min_distance=1.0):
    if (len(origin) != 3 or len(destination) != 3
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in (*origin, *destination))
            or type(min_distance) not in (int, float) or not math.isfinite(min_distance) or min_distance <= 0):
        raise ValueError('Coordenadas de origem/destino inválidas para direção planar.')
    dx = float(destination[0]) - float(origin[0])
    dy = float(destination[1]) - float(origin[1])
    hypot = math.hypot(dx, dy)
    if hypot < min_distance:
        raise ValueError('Destino já está dentro da distância planar mínima.')
    return (dx / hypot, dy / hypot, 0.0)


def build_gather_targets(raw_targets, *, professions=None, require_mastery=True, allow_passive_target=False):
    allowed = frozenset(professions) if professions is not None else None
    result = []
    for entry in raw_targets:
        distance = entry.get('distance_engine_units')
        interaction_range = entry.get('interaction_range_engine_units')
        if distance is None or interaction_range is None:
            continue
        profession = entry.get('profession')
        if allowed is not None and profession not in allowed:
            continue
        mastery_ok = entry.get('mastery_requirement_met')
        available = (mastery_ok is True) if require_mastery else True
        pos_entry = entry.get('position') or {}
        pos = tuple(pos_entry['position']) if pos_entry.get('position_observed') else None
        raw_name = entry['source']['name']
        friendly_name = resolve_gather_name(raw_name)
        eff_range = max(float(interaction_range), 180.0) if allow_passive_target else float(interaction_range)
        result.append(ActivityTarget(
            identity=format_gather_identity(entry),
            name=friendly_name,
            raw_name=raw_name,
            profession=profession,
            distance=float(distance),
            interaction_range=eff_range,
            available=available,
            position=pos,
        ))
    return tuple(result)


class ActivityRevisionTracker:
    """Increments revision only when confirmed world state changes."""

    def __init__(self):
        self.session = None
        self.revision = 0
        self.fingerprint = None

    @staticmethod
    def _canonical(session, *, alive, in_combat, busy, inventory_full,
                   position=None, gatherables=(), quests=(), inventory=(), completed_gathers=(),flight_mode=None):
        pos_key = None
        if position is not None and len(position) == 3 and all(math.isfinite(v) for v in position):
            pos_key = tuple(round(float(v) / 5.0) for v in position)
        gather_key = tuple(sorted(
            (t.identity, t.name, t.available, round(t.distance / 5.0), round(t.interaction_range))
            for t in gatherables if t.valid
        ))
        quest_key = tuple(sorted(
            (q.identity, q.state, q.objective, q.progress, q.target.identity if q.target else None,
             round(q.target.distance / 5.0) if q.target and q.target.valid else None)
            for q in quests
        ))
        inv_key = tuple(sorted(inventory))
        return (session, bool(alive), bool(in_combat), bool(busy), bool(inventory_full),
                pos_key, gather_key, quest_key, inv_key, tuple(sorted(completed_gathers)),flight_mode)

    def observe(self, session, observed_at, *, connected=True, alive=True,
                in_combat=False, busy=False, inventory_full=False,
                position=None, gatherables=(), quests=(), inventory=(), completed_gathers=(),flight_mode=None):
        if flight_mode is not None and (type(flight_mode) is not int or not 0<=flight_mode<=6):
            raise ValueError('Modo de movimento observado inválido.')
        current = self._canonical(
            session, alive=alive, in_combat=in_combat, busy=busy,
            inventory_full=inventory_full, position=position,
            gatherables=gatherables, quests=quests, inventory=inventory,
            completed_gathers=completed_gathers,
            flight_mode=flight_mode,
        )
        if session != self.session:
            self.session = session
            self.revision = 1
            self.fingerprint = current
        elif current != self.fingerprint:
            self.revision += 1
            self.fingerprint = current
        return ActivityObservation(
            session=session,
            observed_at=observed_at,
            revision=self.revision,
            connected=connected,
            alive=alive,
            in_combat=in_combat,
            busy=busy,
            inventory_full=inventory_full,
            gatherables=tuple(gatherables),
            quests=tuple(quests),
            completed_gathers=tuple(completed_gathers),
            flight_mode=flight_mode,
        )


@dataclass(frozen=True)
class WorldFrame:
    observation: ActivityObservation
    player_position: tuple[float, float, float] | None = None
    target_positions: dict[str, tuple[float, float, float]] | None = None
    gather_evidence: tuple[GatherEvidence, ...] = ()
    hud_context: tuple | None = None  # verified interactions, progress, source history
    flight_state: dict | None = None


class ActivityAdapter:
    """Executes ActivityPolicy commands with strict pre-dispatch re-verification."""

    def __init__(self, policy: ActivityPolicy, observer, dispatcher, *, combat_active=lambda: False,
                 cancelled=lambda: False, clock=None, confirmation='inventory'):
        if confirmation not in ('inventory','hud'):raise ValueError('Confirmação de coleta desconhecida.')
        self.confirmation=confirmation
        self.policy = policy
        self.observer = observer
        self.dispatcher = dispatcher
        self.combat_active = combat_active
        self.cancelled = cancelled
        self.clock = clock
        self.receipt = None
        self.receipt_sent_at = None
        self.completed_gathers = {}
        self.receipt_session = None
        self.hud_attempt=None
        self.last_collection_result=None
        self._gather_pending_since = None
        self._gather_pending_target = None
        self._navigate_pending_since = None
        self._navigate_pending_target = None

    def _observe_receipt(self, frame, now):
        obs = frame.observation
        if self.receipt_session != obs.session:
            self.completed_gathers.clear()
            self.receipt_session = obs.session
        if self.hud_attempt is not None:
            target_id,attempt,scope=self.hud_attempt
            if obs.session!=scope or frame.hud_context is None:
                outcome='unconfirmed'
            else:
                interactions,progress,_=frame.hud_context
                outcome=attempt.observe(interactions,progress,observed_at=obs.observed_at,now=now)
            if outcome=='hud_success':
                self.completed_gathers[target_id]=self.completed_gathers.get(target_id,0)+1
                self.last_collection_result=outcome
                self.hud_attempt=None
            elif outcome!='waiting':
                self.policy.stop()
                self.policy.status=('A coleta falhou no HUD; rotina interrompida' if outcome=='hud_failure'
                                    else 'Término de coleta não confirmado pelo HUD; rotina interrompida')
                self.last_collection_result=outcome
                self.hud_attempt=None
        if self.receipt is not None:
            evidence = next((e for e in frame.gather_evidence
                             if e.source_identity == self.receipt.before.source_identity), None)
            expired = now - self.receipt_sent_at >= self.policy.timeout
            if obs.session != self.receipt.before.session:
                outcome = GatherOutcome.WRONG_SESSION
            elif evidence is None:
                outcome = GatherOutcome.UNCONFIRMED if expired else GatherOutcome.WAITING
            elif (not math.isfinite(evidence.observed_at)
                    or not 0 <= now - evidence.observed_at <= self.policy.max_age):
                outcome = GatherOutcome.UNCONFIRMED if expired else GatherOutcome.WAITING
            else:
                outcome = self.receipt.observe(evidence, expired=expired)
                if expired and outcome == GatherOutcome.WAITING:
                    outcome = GatherOutcome.UNCONFIRMED
            if outcome == GatherOutcome.SUCCESS:
                key = self.receipt.before.source_identity
                self.completed_gathers[key] = self.completed_gathers.get(key, 0) + 1
                self.receipt = None
            elif outcome != GatherOutcome.WAITING:
                self.policy.stop()
                self.policy.status = ('A coleta falhou no jogo; rotina interrompida'
                                      if outcome==GatherOutcome.FAILURE
                                      else 'Extração sem comprovante válido; rotina interrompida')
                self.receipt = None
        counts = dict(obs.completed_gathers)
        for key, value in self.completed_gathers.items():
            counts[key] = counts.get(key, 0) + value
        return replace(frame, observation=replace(obs,
            revision=obs.revision + sum(self.completed_gathers.values()),
            completed_gathers=tuple(sorted(counts.items()))))

    def tick(self, now):
        if self.cancelled():
            self.policy.stop()
            return None
        if self.policy.check_deadline(now):return None
        if self.combat_active():
            self.policy.status = 'Aguardando controlador de combate encerrar'
            return None
        frame = self.observer(now)
        if self.clock is not None:now = self.clock()
        frame = self._observe_receipt(frame, now)
        obs = frame.observation
        command = self.policy.pending or self.policy.step(obs, now)
        if command is None:
            return None
        reverified = self.observer(now)
        if self.clock is not None:now = self.clock()
        reverified = self._observe_receipt(reverified, now)
        if self.policy.check_deadline(now):
            status = self.policy.status
            self.policy.acknowledge(command, False, now)
            self.policy.status = status
            return command
        reobs = reverified.observation
        if (not reobs.connected or reobs.session != obs.session or not reobs.alive
                or not math.isfinite(reobs.observed_at)
                or not 0 <= now - reobs.observed_at <= self.policy.max_age
                or reobs.observed_at < obs.observed_at or reobs.revision < obs.revision
                or reobs.in_combat or reobs.busy or reobs.inventory_full or self.combat_active()
                or self.cancelled()):
            reasons = []
            if not reobs.connected: reasons.append('not_connected')
            if reobs.session != obs.session: reasons.append(f'session_mismatch({reobs.session}!={obs.session})')
            if not reobs.alive: reasons.append('not_alive')
            if not math.isfinite(reobs.observed_at): reasons.append('nan_observed_at')
            if not 0 <= now - reobs.observed_at <= self.policy.max_age: reasons.append(f'max_age_exceeded(now-obs={now-reobs.observed_at:.2f}>max={self.policy.max_age})')
            if reobs.observed_at < obs.observed_at: reasons.append('observed_at_regression')
            if reobs.revision < obs.revision: reasons.append('revision_regression')
            if reobs.in_combat: reasons.append('in_combat')
            if reobs.busy: reasons.append('busy')
            if reobs.inventory_full: reasons.append('inventory_full')
            if self.combat_active(): reasons.append('combat_active')
            if self.cancelled(): reasons.append('cancelled')
            self.policy.acknowledge(command, False, now, reason='; '.join(reasons))
            return command
        if isinstance(command.argument, tuple):
            quest_id = command.argument[0]
            previous = next((q for q in obs.quests if q.identity == quest_id), None)
            current = next((q for q in reobs.quests if q.identity == quest_id), None)
            if (previous is None or current is None or previous.state != current.state
                    or previous.objective != current.objective or current.target is None
                    or current.target.identity != command.argument[1]):
                self.policy.acknowledge(command, False, now, reason='quest_state_mismatch')
                return command
        target_id = command.argument[1] if isinstance(command.argument, tuple) else command.argument
        target = self._find_target(reobs, target_id)
        if target is None or not target.valid:
            if command.operation == 'navigate':
                if self._navigate_pending_target != target_id or self._navigate_pending_since is None:
                    self._navigate_pending_since = now
                    self._navigate_pending_target = target_id
                if now - self._navigate_pending_since < 3.5:
                    return command
                self._navigate_pending_since = None
            reason = f'target_not_found_or_invalid(target_id={target_id}, found={target is not None}, valid={target.valid if target else False})'
            self.policy.acknowledge(command, False, now, reason=reason)
            return command
        if command.operation != 'gather':
            self._gather_pending_since = None
            self._gather_pending_target = None
        succeeded = False
        if command.operation == 'navigate':
            if target.distance <= target.interaction_range:
                acknowledged_at = self.clock() if self.clock is not None else now
                self.policy.acknowledge(command, True, acknowledged_at)
                return command
            if (reverified.player_position is not None
                    and reverified.target_positions
                    and target_id in reverified.target_positions):
                try:
                    route=approach_3d(reverified.player_position,
                        reverified.target_positions[target_id],target.interaction_range)
                    flying=(reverified.flight_state or {}).get('movement',{}).get('MovementMode')==5
                    supports_flight=getattr(self.dispatcher,'supports_flight',False) is True
                    if flying and not supports_flight:
                        self.policy.acknowledge(command,False,now)
                        self.policy.status='Movimento aéreo ainda sem validação completa; rotina interrompida'
                        return command
                    is_aerial = requires_flight(getattr(target, 'profession', ''), reverified.player_position,
                        reverified.target_positions[target_id], target.interaction_range)
                    if is_aerial and not supports_flight:
                        self.policy.acknowledge(command,False,now)
                        self.policy.status='Destino exige controle vertical; voo ainda não validado'
                        return command
                    if is_aerial and route.requires_vertical_control and supports_flight and reobs.flight_mode==1:
                        reader = getattr(getattr(self.dispatcher, 'transport', None), 'reader', None)
                        if reader is not None and hasattr(reader, 'flight_stamina_ratio'):
                            try:
                                ratio = reader.flight_stamina_ratio()
                                if ratio is not None and ratio < 0.60:
                                    self.policy.status = f'Aguardando regeneração de estamina de voo ({int(ratio*100)}% < 60%)'
                                    return None
                            except Exception:
                                pass
                        command=self.policy.prepare_takeoff(command)
                        succeeded=bool(self.dispatcher.takeoff(target_id,session=reobs.session))
                        acknowledged_at=self.clock() if self.clock is not None else now
                        self.policy.acknowledge(command,succeeded,acknowledged_at)
                        return command
                    direction = route.direction if supports_flight and (flying or (route.requires_vertical_control and is_aerial)) else planar_direction(
                        reverified.player_position,
                        reverified.target_positions[target_id],
                    )
                except ValueError as err:
                    if 'Destino já está dentro da distância planar mínima' in str(err):
                        acknowledged_at = self.clock() if self.clock is not None else now
                        self.policy.acknowledge(command, True, acknowledged_at)
                        return command
                    self.policy.acknowledge(command, False, now)
                    return command
                succeeded = bool(self.dispatcher.navigate(
                    target_id, direction=direction, session=reobs.session))
                if not succeeded:
                    if self._navigate_pending_target != target_id or self._navigate_pending_since is None:
                        self._navigate_pending_since = now
                        self._navigate_pending_target = target_id
                    if now - self._navigate_pending_since < 3.5:
                        return command
                    self._navigate_pending_since = None
                    self.policy.acknowledge(command, False, now, reason='dispatcher_navigate_failed')
                    return command
                self._navigate_pending_since = None
                self._navigate_pending_target = None
        elif command.operation == 'gather':
            effective_range = max(target.interaction_range, 250.0 if getattr(self, 'allow_passive_target', False) else 160.0)
            if target.distance > effective_range:
                acknowledged_at = self.clock() if self.clock is not None else now
                self.policy.acknowledge(command, True, acknowledged_at)
                return command
            if target.distance <= effective_range:
                if self.confirmation=='hud':
                    context=reverified.hud_context
                    if context is None:
                        if self._gather_pending_target != target_id or self._gather_pending_since is None:
                            self._gather_pending_since = now
                            self._gather_pending_target = target_id
                        if now - self._gather_pending_since < 3.5:
                            return command
                        self._gather_pending_since = None
                        self.policy.acknowledge(command,False,now,reason='hud_context_is_none')
                        return command
                    interactions,progress,sources=context
                    candidates=[s for s in sources if s.identity==target_id]
                    if len(candidates)!=1:
                        if self._gather_pending_target != target_id or self._gather_pending_since is None:
                            self._gather_pending_since = now
                            self._gather_pending_target = target_id
                        if now - self._gather_pending_since < 3.5:
                            return command
                        self._gather_pending_since = None
                        self.policy.acknowledge(command,False,now,reason=f'candidates_count={len(candidates)}')
                        return command
                    source=candidates[0]
                    attempt=GatherHudAttempt(source.source_id,source.key,interactions,progress,
                        observed_at=reobs.observed_at,now=now,timeout=self.policy.timeout,max_age=self.policy.max_age)
                    succeeded=bool(self.dispatcher.gather(target_id,session=reobs.session))
                    acknowledged_at=self.clock() if self.clock is not None else now
                    if succeeded:
                        self._gather_pending_since = None
                        self._gather_pending_target = None
                        fresh=getattr(self.dispatcher,'last_gather_baseline',None)
                        dispatched_at=getattr(self.dispatcher,'last_gather_dispatched_at',None)
                        if isinstance(fresh,tuple) and len(fresh)==3 and dispatched_at is not None:
                            fresh_interactions,fresh_progress,captured_at=fresh
                            attempt=GatherHudAttempt(source.source_id,source.key,fresh_interactions,fresh_progress,
                                observed_at=captured_at,now=dispatched_at,timeout=self.policy.timeout,max_age=self.policy.max_age)
                            attempt.submitted(acknowledged_at,dispatched_at=dispatched_at)
                        else:attempt.submitted(acknowledged_at)
                        self.hud_attempt=(target_id,attempt,reobs.session)
                        self.policy.acknowledge(command,succeeded,acknowledged_at)
                        return command
                    else:
                        if self._gather_pending_target != target_id or self._gather_pending_since is None:
                            self._gather_pending_since = now
                            self._gather_pending_target = target_id
                        if now - self._gather_pending_since < 3.5:
                            return command
                        self._gather_pending_since = None
                        self.policy.acknowledge(command, False, acknowledged_at, reason='dispatcher_gather_failed')
                        return command
                evidence = next((e for e in reverified.gather_evidence
                                 if e.source_identity == target_id), None)
                receipt = None
                if evidence is not None:
                    if (evidence.session != reobs.session or not math.isfinite(evidence.observed_at)
                            or not 0 <= now - evidence.observed_at <= self.policy.max_age):
                        self.policy.acknowledge(command, False, now, reason='evidence_max_age_or_session_mismatch')
                        return command
                    receipt = GatherReceipt(evidence)
                if receipt is not None or not getattr(self.dispatcher, 'requires_gather_evidence', False):
                    succeeded = bool(self.dispatcher.gather(target_id, session=reobs.session))
                if succeeded and receipt is not None:
                    self.receipt, self.receipt_sent_at = receipt, now
        elif command.operation in ('interact', 'accept_quest', 'turn_in_quest'):
            if target.distance <= target.interaction_range:
                succeeded = bool(self.dispatcher.interact_quest(
                    command.operation, command.argument[0], target_id, session=reobs.session))
        acknowledged_at = self.clock() if self.clock is not None else now
        self.policy.acknowledge(command, succeeded, acknowledged_at, reason=f'end_of_tick(op={command.operation},succeeded={succeeded})')
        return command

    @staticmethod
    def _find_target(obs: ActivityObservation, target_id: str):
        for item in obs.gatherables:
            if item.identity == target_id:
                return item
        for quest in obs.quests:
            if quest.target and quest.target.identity == target_id:
                return quest.target
        return None


# Approximate travel speeds (engine units/s) used only to size movement pulses.
GROUND_SPEED = 600.0
FLIGHT_SPEED = 800.0


class LiveActivityDispatcher:
    """Dispatches verified movement pulses and collect actions via MacroTransport."""
    requires_gather_evidence = True

    def __init__(self, transport, *, duration_ms=200, cancelled=lambda:False,flight_enabled=False,chain_pulses=1,max_age=1.0,allow_passive_target=False):
        self.transport = transport
        self.duration_ms = min(max(duration_ms, 50), 1500)
        self.chain_pulses = max(1, int(chain_pulses))
        self.cancelled = cancelled
        self.supports_flight=flight_enabled is True
        self.max_age = float(max_age)
        self.allow_passive_target = allow_passive_target is True
        self.last_gather_baseline=None;self.last_gather_dispatched_at=None

    def _ready(self, snapshot, session):
        allow = getattr(self, 'allow_passive_target', False) if not isinstance(self, type) else False
        if snapshot.scope != session or snapshot.player_health <= 0 or snapshot.macro_active:
            return False
        if not allow and (snapshot.target is not None and snapshot.target.eligible and snapshot.target.health > 0):
            return False
        return True

    def _pulse_ms(self, travel, speed):
        # Long enough to reach running speed, short enough to re-aim/re-check often.
        return int(min(self.duration_ms, max(250.0, travel / speed * 1000.0)))

    def navigate(self, target_id, *, direction, session):
        snapshot = self.transport.snapshot()
        if not self._ready(snapshot,session) or self.cancelled():
            return False
        if self.supports_flight:
            return self._navigate_flight(target_id,snapshot,session)
        completed = False
        for _ in range(self.chain_pulses):
            if self.cancelled():
                break
            reply = self.transport.movement_pulse(
                snapshot, direction, duration_ms=self.duration_ms, execute=True)
            completed = bool(reply.get('movement_pulse_completed'))
            if not completed:
                break
        return completed

    def _navigate_flight(self,target_id,snapshot,session):
        from .navigation3d import plan_gather_approach
        reader=self.transport.reader
        def route():
            report=reader.gather_sources()
            if (report.get('session')!=reader.session or tuple(report.get('player_identity') or ())!=snapshot.player_identity):
                raise SnapshotUnavailable('Personagem mudou antes da aproximação.')
            entries=[r for r in report.get('targets',()) if format_gather_identity(r)==target_id]
            if len(entries)!=1:raise SnapshotUnavailable('Recurso deixou de estar disponível.')
            _,geometry=plan_gather_approach(report,target_identity=entries[0]['identity'])
            return geometry, entries[0], (report.get('player_position') or {}).get('position')
        geometry, entry, start = route()
        try:
            is_aerial = requires_flight(
                entry.get('profession') or (entry.get('source') or {}).get('profession'),
                start, entry['position']['position'], geometry.interaction_range)
        except (KeyError, TypeError, IndexError):
            is_aerial = geometry.requires_vertical_control
        close_threshold = max(geometry.interaction_range, 160.0) if getattr(self, 'allow_passive_target', False) else geometry.interaction_range
        if geometry.distance <= close_threshold: return True
        state=reader.flight_state()
        if state['session']!=reader.session or tuple(state['player_identity'])!=snapshot.player_identity:return False
        mode=state['movement']['MovementMode']
        if mode==1 and geometry.requires_vertical_control and is_aerial:
            return False  # takeoff is acknowledged independently of approach
        if self.cancelled():return False
        if mode==5:
            direction=geometry.direction
            norm=math.hypot(*direction)
            if norm<.000001:return True
            dir_unit = tuple(v/norm for v in direction)
            completed = False
            target_close_range = max(95.0, min(120.0, geometry.interaction_range - 10.0))
            needed_travel = max(0.0, geometry.distance - target_close_range)
            if needed_travel <= 0.0:
                return True
            # One continuous pulse sized to the remaining distance: the character keeps
            # its speed instead of restarting from zero every 200 ms ("dragging").
            reply = self.transport.flight_movement_pulse(snapshot, dir_unit,
                duration_ms=self._pulse_ms(needed_travel, FLIGHT_SPEED), execute=True)
            return bool(reply.get('movement_pulse_completed'))
        elif mode==1 and (not geometry.requires_vertical_control or not is_aerial):
            direction=(geometry.direction[0],geometry.direction[1],0.)
            norm=math.hypot(*direction)
            if norm<.000001:return True
            dir_unit = tuple(v/norm for v in direction)
            completed = False
            target_close_range = max(95.0, min(140.0, geometry.interaction_range - 10.0))
            needed_travel = max(0.0, geometry.distance - target_close_range)
            if needed_travel <= 0.0:
                return True
            duration = self._pulse_ms(needed_travel, GROUND_SPEED)
            try:
                reply = self.transport.movement_pulse(
                    snapshot, dir_unit, duration_ms=duration, execute=True, stop_macro=True)
            except TypeError:
                reply = self.transport.movement_pulse(snapshot, dir_unit, duration_ms=duration, execute=True)
            return bool(reply.get('movement_pulse_completed'))
        else:return False
        return bool(reply.get('movement_pulse_completed'))

    def takeoff(self,target_id,*,session):
        from .navigation3d import plan_gather_approach
        snapshot=self.transport.snapshot()
        if not self.supports_flight or not self._ready(snapshot,session) or self.cancelled():return False
        reader=self.transport.reader;report=reader.gather_sources()
        if (report.get('session')!=reader.session or tuple(report.get('player_identity') or ())!=snapshot.player_identity):return False
        targets=[r for r in report.get('targets',()) if format_gather_identity(r)==target_id]
        if len(targets)!=1:return False
        _,geometry=plan_gather_approach(report,target_identity=targets[0]['identity'])
        if not geometry.requires_vertical_control:return False
        interactions=reader.interactions()
        scope=GatherHudAttempt._scope(interactions)
        if (scope[0]!=reader.session or scope[1]!=snapshot.player_identity
                or scope[2]!=tuple(report.get('character_session_identity') or ())
                or active_interactions(GatherHudAttempt._actions(interactions))):return False
        state=reader.flight_state()
        if state['session']!=reader.session or tuple(state['player_identity'])!=snapshot.player_identity:return False
        if hasattr(reader, 'flight_stamina_ratio'):
            try:
                stamina_ratio = reader.flight_stamina_ratio()
                if stamina_ratio is not None and stamina_ratio < 0.60:
                    return False
            except Exception:
                pass
        if state['movement']['MovementMode']==5:return True
        if state['movement']['MovementMode']!=1 or self.cancelled():return False
        succeeded=bool(self.transport.flight_takeoff(snapshot))
        return succeeded and not self.cancelled()

    def gather(self, target_id, *, session):
        self.last_gather_baseline=None;self.last_gather_dispatched_at=None
        started=time.monotonic()
        snapshot = self.transport.snapshot()
        if not self._ready(snapshot,session) or self.cancelled():
            return False
        reader=self.transport.reader
        report=reader.gather_sources()
        if (report.get('player_present') is not True or report.get('session') != reader.session
                or tuple(report.get('player_identity',())) != snapshot.player_identity):
            return False
        candidates=[entry for entry in report.get('targets',()) if format_gather_identity(entry)==target_id]
        if len(candidates)!=1:return False
        entry=candidates[0]
        character=report.get('character_session_identity')
        if not isinstance(character,(tuple,list)) or len(character)!=2:return False
        targets=build_gather_targets((entry,))
        effective_range = max(targets[0].interaction_range if targets else 130.0, 250.0 if getattr(self, 'allow_passive_target', False) else 160.0)
        if len(targets)!=1 or not targets[0].valid or targets[0].distance > effective_range:
            return False
        key=entry.get('object_key')
        if not isinstance(key,(tuple,list)) or len(key)!=3:return False
        context=reader.contextual()
        if (context.get('layout_code_validated') is not True or context.get('selection_enabled') is not True
                or context.get('resolved_action') not in (197,200)
                or context.get('session') != reader.session
                or tuple(context.get('player_identity',())) != snapshot.player_identity
                or tuple(context.get('character_session_identity') or ()) != tuple(character)):
            return False
        if not getattr(self, 'allow_passive_target', False):
            if tuple(context.get('selected_key') or ()) != tuple(key):
                return False
            if context.get('dispatch_eligible') is not True or context.get('prompt_verified') is not True:
                return False
        else:
            sel = tuple(context.get('selected_key') or ())
            if sel and sel != tuple(key) and not any(tuple(r.get('object_key') or ()) == sel for r in report.get('targets', ())):
                return False
        fresh_report=reader.gather_sources()
        fresh_targets=[row for row in fresh_report.get('targets',()) if format_gather_identity(row)==target_id]
        if (fresh_report.get('player_present') is not True or fresh_report.get('session')!=reader.session
                or tuple(fresh_report.get('player_identity') or ())!=snapshot.player_identity
                or tuple(fresh_report.get('character_session_identity') or ())!=tuple(character)
                or len(fresh_targets)!=1 or tuple(fresh_targets[0].get('object_key') or ())!=tuple(key)):
            return False
        available=build_gather_targets(fresh_targets)
        fresh_effective_range = max(available[0].interaction_range if available else 130.0, 250.0 if getattr(self, 'allow_passive_target', False) else 160.0)
        if len(available)!=1 or not available[0].valid or available[0].distance>fresh_effective_range:
            return False
        baseline_started=time.monotonic()
        interactions=reader.interactions()
        progress=reader.gather_progress()
        try:
            scope=GatherHudAttempt._scope(interactions)
            actions=GatherHudAttempt._actions(interactions)
            if (scope!=(reader.session,snapshot.player_identity,tuple(character))
                    or active_interactions(actions) or progress.session!=reader.session
                    or progress.stage not in ('hud_absent','progress_absent','progress_hidden')):
                return False
            progress.result_kind()
        except (ValueError,TypeError):return False
        rechecked=self.transport.snapshot()
        now=time.monotonic()
        verification_started=context.get('verification_started_at')
        verified_at=context.get('verified_at')
        if verification_started is None and verified_at is None:
            fresh_context=0<=now-started<=self.max_age
        else:
            fresh_context=(all(type(t) in (int,float) and math.isfinite(t) for t in (verification_started,verified_at))
                and 0<=now-verification_started<=self.max_age and verification_started<=verified_at<=now)
        if (not self._ready(rechecked,session) or not fresh_context
                or self.cancelled()):return False
        self.last_gather_baseline=(interactions,progress,baseline_started)
        self.last_gather_dispatched_at=time.monotonic()
        return bool(self.transport.collect(rechecked))


class LiveActivityObserver:
    """Builds WorldFrame observations from MacroReader for ActivityAdapter."""

    def __init__(self, reader, *, professions=None, require_mastery=True, confirmation='inventory',flight_enabled=False,allow_passive_target=False):
        if confirmation not in ('inventory','hud'):raise ValueError('Confirmação de coleta desconhecida.')
        self.confirmation=confirmation
        self.flight_enabled=flight_enabled is True
        self.allow_passive_target = allow_passive_target is True
        self.reader = reader
        self.professions = professions
        self.require_mastery = require_mastery
        self.revisions = ActivityRevisionTracker()
        self.source_history = GatherSourceHistory()

    def __call__(self, now):
        for attempt in range(2):
            try:return self._read_frame(now)
            except (RegistryChanged,NamePoolChanged):
                if attempt:raise

    def _read_frame(self, now):
        snapshot = self.reader.snapshot()
        gather_report = self.reader.gather_sources()
        if not gather_report.get('player_present'):
            obs = self.revisions.observe(
                self.reader.session or '', now, connected=False, alive=False)
            return WorldFrame(obs)
        if (gather_report.get('session') != self.reader.session
                or tuple(gather_report.get('player_identity', ())) != snapshot.player_identity):
            raise SnapshotUnavailable('Personagem mudou durante a leitura dos recursos.')
        progress = self.reader.gather_progress()
        if progress.session != self.reader.session:
            raise SnapshotUnavailable('Sessão mudou durante a leitura do progresso de coleta.')
        inventory=self.reader.inventory() if self.confirmation=='inventory' else None
        interactions=self.reader.interactions()
        flight_state=self.reader.flight_state() if self.flight_enabled else None
        if flight_state is not None and (flight_state['session']!=self.reader.session
                or tuple(flight_state['player_identity'])!=snapshot.player_identity):
            raise SnapshotUnavailable('Personagem mudou durante a leitura do voo.')
        rechecked = self.reader.snapshot()
        if rechecked.scope != snapshot.scope:
            raise SnapshotUnavailable('Personagem mudou durante a leitura de coleta.')
        player_pos_info = gather_report.get('player_position') or {}
        player_pos = (tuple(player_pos_info['position'])
                      if player_pos_info.get('position_observed') else None)
        raw_targets = gather_report.get('targets', ())
        sources=self.source_history.observe(snapshot.scope,raw_targets)
        if self.confirmation=='inventory':
            evidence,totals=build_gather_evidence(snapshot,self.reader.session,gather_report,
                inventory,interactions,progress,sources)
        else:
            scope=GatherHudAttempt._scope(interactions)
            GatherHudAttempt._actions(interactions)
            if (scope[0]!=self.reader.session or scope[1]!=snapshot.player_identity
                    or scope[2]!=tuple(gather_report.get('character_session_identity') or ())):
                raise SnapshotUnavailable('Personagem/sessão mudou durante a leitura do HUD.')
            progress.result_kind()
            evidence,totals=(),()
        gatherables = build_gather_targets(
            raw_targets, professions=self.professions, require_mastery=self.require_mastery,
            allow_passive_target=self.allow_passive_target)
        target_positions = {}
        for entry in raw_targets:
            pos_info = entry.get('position') or {}
            if pos_info.get('position_observed'):
                target_positions[format_gather_identity(entry)] = tuple(pos_info['position'])
        in_combat = bool(rechecked.macro_active or (
            not getattr(self, 'allow_passive_target', False)
            and rechecked.target is not None and rechecked.target.eligible and rechecked.target.health > 0))
        obs = self.revisions.observe(
            snapshot.scope,
            rechecked.captured_at if hasattr(rechecked, 'captured_at') else snapshot.captured_at,
            connected=True,
            alive=rechecked.player_health > 0,
            in_combat=in_combat,
            busy=progress.stage == 'progress_visible' or bool(active_interactions(interactions.get('actions',()))),
            inventory_full=inventory['free_slots']==0 if inventory is not None else False,
            position=player_pos,
            gatherables=gatherables,
            inventory=totals,
            flight_mode=flight_state['movement']['MovementMode'] if flight_state is not None else None,
        )
        return WorldFrame(obs, player_position=player_pos, target_positions=target_positions,gather_evidence=evidence,
                          hud_context=(interactions,progress,sources) if self.confirmation=='hud' else None,
                          flight_state=flight_state)
