"""Quest/gather decisions independent of game IO.

Commands require both delivery acknowledgement and a newer observation showing
progress. A successful send alone never means a quest or collection succeeded.
"""
from dataclasses import dataclass
import math

from .core import Command


@dataclass(frozen=True)
class ActivityTarget:
    identity: str
    name: str
    distance: float
    interaction_range: float
    available: bool = True
    position: tuple[float, float, float] | None = None
    raw_name: str = ''
    profession: str | None = None

    @property
    def valid(self):
        return (bool(self.identity) and math.isfinite(self.distance)
                and self.distance >= 0 and math.isfinite(self.interaction_range)
                and self.interaction_range > 0 and self.available is True)


@dataclass(frozen=True)
class Quest:
    identity: str
    state: str  # available, active, ready, completed
    target: ActivityTarget | None = None
    objective: str = ''  # gather, interact, kill; kill needs combat integration
    progress: tuple[int, ...] = ()


@dataclass(frozen=True)
class ActivityObservation:
    session: str
    observed_at: float
    revision: int
    connected: bool = True
    alive: bool = True
    in_combat: bool = False
    busy: bool = False
    inventory_full: bool = False
    gatherables: tuple[ActivityTarget, ...] = ()
    quests: tuple[Quest, ...] = ()
    completed_gathers: tuple[tuple[str, int], ...] = ()
    flight_mode: int | None = None


class ActivityPolicy:
    """Single routine, one outstanding action, bounded observation wait.

    The adapter must increment revision only on confirmed relevant world state
    changes (movement, objective count, node availability, quest state), never
    just because it polled. Commands contain stable identities, not coordinates
    or pointers from a previous session.
    """

    def __init__(self, mode, *, names=(), exclude=False, max_age=1., timeout=15.,min_navigation_progress=1.,
                 center=None, radius=None):
        if mode not in ('gathering', 'quests'):
            raise ValueError('Rotina desconhecida.')
        if not all(type(x) in (int,float) and math.isfinite(x) and x > 0
                   for x in (max_age, timeout,min_navigation_progress)):
            raise ValueError('Prazos inválidos.')
        if radius is not None and (type(radius) not in (int, float) or not math.isfinite(radius) or radius <= 0):
            raise ValueError('Raio inválido.')
        if center is not None and (not isinstance(center, (tuple, list)) or len(center) not in (2, 3) or not all(type(v) in (int, float) and math.isfinite(v) for v in center)):
            raise ValueError('Centro inválido.')
        self.mode, self.names, self.exclude = mode, frozenset(names), exclude
        self.max_age, self.timeout = max_age, timeout
        self.center = tuple(center) if center is not None else None
        self.radius = float(radius) if radius is not None else None
        self.running = False
        self.pending = None
        self.session = None
        self.awaiting = None
        self.navigation_target = None
        self.navigation_progress=None
        self.min_navigation_progress=min_navigation_progress
        self.status = 'Parado'

    def start(self):
        if self.running or self.pending or self.awaiting:
            raise RuntimeError('Confirme a conclusão da ação antes de reiniciar.')
        self.running, self.session = True, None
        self.navigation_target = None
        self.navigation_progress=None
        self.status = 'Aguardando leitura da rotina'

    def stop(self):
        self.running = False
        self.status = 'Parado; ações já enviadas precisam ser conferidas pelo adaptador'

    def acknowledge(self, command, succeeded, now, reason=None):
        if command != self.pending or self.pending is None:
            raise RuntimeError('Confirmação não corresponde à ação pendente.')
        self.pending = None
        if succeeded is not True:
            self.running = False
            self.status = 'Ação não confirmada; confira o jogo antes de reiniciar'
            self.awaiting = None
            try:
                import traceback
                from pathlib import Path
                log_p = Path(__file__).resolve().parent.parent / 'data' / 'last_error.log'
                log_p.parent.mkdir(exist_ok=True)
                with log_p.open('a', encoding='utf-8') as f:
                    f.write(f'--- ACKNOWLEDGE FAILED ---\ncommand: {command}\nsucceeded: {succeeded}\nreason: {reason}\n')
                    traceback.print_stack(file=f)
            except Exception:
                pass
        else:
            self.awaiting = (self._revision, self._observed_at, now)

    def prepare_takeoff(self,command):
        if (command!=self.pending or command.operation!='navigate'
                or type(self._before.flight_mode) is not int or self._before.flight_mode!=1):
            raise RuntimeError('Decolagem exige navegação pendente e modo terrestre observado.')
        self.pending=Command('takeoff',command.argument)
        self._last_command=self.pending
        self.navigation_progress=None
        self.status='Aguardando decolagem normal'
        return self.pending

    def _made_progress(self, obs):
        command, before = self._last_command, self._before
        argument = command.argument
        target_id = argument[1] if isinstance(argument, tuple) else argument
        if command.operation=='takeoff':
            return before.flight_mode==1 and type(obs.flight_mode) is int and obs.flight_mode==5
        if command.operation == 'navigate':
            def distance(frame):
                targets = list(frame.gatherables) + [q.target for q in frame.quests if q.target]
                return next((t.distance for t in targets if t.identity == target_id and t.valid), None)
            old, new = distance(before), distance(obs)
            return old is not None and new is not None and new < old
        if isinstance(argument, tuple):
            previous = next((q for q in before.quests if q.identity == argument[0]), None)
            current = next((q for q in obs.quests if q.identity == argument[0]), None)
            if previous is not None and current is not None:
                if command.operation == 'accept_quest':
                    return previous.state == 'available' and current.state in ('active', 'ready', 'completed')
                if command.operation == 'turn_in_quest':
                    return previous.state == 'ready' and current.state == 'completed'
                if (current.state in ('ready', 'completed')
                        or (len(current.progress) == len(previous.progress)
                            and all(a >= b for a, b in zip(current.progress, previous.progress))
                            and any(a > b for a, b in zip(current.progress, previous.progress)))):
                    return True
        if command.operation == 'gather':
            return dict(obs.completed_gathers).get(target_id, 0) > dict(before.completed_gathers).get(target_id, 0)
        return False

    def check_deadline(self, now):
        if not self.running:return False
        if math.isfinite(now):
            if self.navigation_progress and now-self.navigation_progress[2]>=self.timeout:
                self.running=False
                self.status='Aproximação sem avanço suficiente; rotina interrompida'
                return True
            if self.pending and now - self._issued_at >= self.timeout:
                self.running = False
                self.status = 'Entrega sem confirmação; confira a ação no jogo'
                return True
            if self.awaiting and now - self.awaiting[2] >= self.timeout:
                self.running = False
                self.status = 'Sem progresso confirmado; rotina interrompida'
                return True
        return False

    def step(self, obs, now):
        if not self.running or self.check_deadline(now):
            return None
        if self.session is not None and obs.session != self.session:
            self.running = False
            self.status = 'Sessão mudou; rotina interrompida'
            return None
        if (not math.isfinite(now) or not math.isfinite(obs.observed_at)
                or not obs.connected or not obs.session
                or not 0 <= now - obs.observed_at <= self.max_age):
            self.status = 'Aguardando leitura atualizada'
            return None
        self.session = obs.session
        if not obs.alive:
            self.running = False
            self.status = 'Personagem morto; rotina interrompida'
            return None
        if self.pending:
            if now - self._issued_at >= self.timeout:
                self.running = False
                self.status = 'Entrega sem confirmação; confira a ação no jogo'
            return None
        if self.awaiting:
            revision, timestamp, sent_at = self.awaiting
            if (obs.revision <= revision or obs.observed_at <= max(timestamp, sent_at)
                    or not self._made_progress(obs)):
                if now - sent_at >= self.timeout:
                    self.running = False
                    self.status = 'Sem progresso confirmado; rotina interrompida'
                return None
            self.awaiting = None
            if self._last_command.operation=='navigate' and self.navigation_progress:
                identity,best,changed_at=self.navigation_progress
                targets=list(obs.gatherables)+[q.target for q in obs.quests if q.target]
                current=next((t for t in targets if t.identity==identity and t.valid),None)
                if current is not None and current.distance<=best-self.min_navigation_progress:
                    self.navigation_progress=(identity,current.distance,obs.observed_at)
        if obs.in_combat or obs.busy or obs.inventory_full:
            self.status = ('Inventário cheio' if obs.inventory_full
                           else 'Aguardando término do combate ou interação')
            return None
        if self.mode == 'gathering':
            def match_gather_target(t):
                if not self.names:
                    return not self.exclude
                matched = any(
                    n.lower() in t.name.lower() or (getattr(t, 'raw_name', '') and n.lower() in getattr(t, 'raw_name', '').lower())
                    for n in self.names
                )
                return (matched != self.exclude)
            targets = [t for t in obs.gatherables if t.valid and match_gather_target(t)]
            if self.center is not None and self.radius is not None:
                def in_bounds(t):
                    if t.position is not None:
                        return math.dist(t.position[:2], self.center[:2]) <= self.radius
                    return t.distance <= self.radius
                targets = [t for t in targets if in_bounds(t)]
            if not targets:
                self.status = 'Aguardando recursos disponíveis dentro da área' if (self.center and self.radius) else 'Aguardando recursos disponíveis'
                return None
            ground_targets = [t for t in targets if getattr(t, 'profession', '') != 'Od']
            target = next((t for t in targets if t.identity == self.navigation_target), None)
            if target is not None and getattr(obs, 'flight_mode', None) == 1 and getattr(target, 'profession', '') == 'Od' and ground_targets:
                target = None
            if target is None:
                pool = ground_targets if (ground_targets and getattr(obs, 'flight_mode', None) == 1) else targets
                target = min(pool, key=lambda t: (t.distance, t.identity))
            operation, argument = 'gather', target.identity
        else:
            quests = [q for q in obs.quests if q.identity and q.target
                      and q.target.valid and q.state in ('available', 'active', 'ready')]
            if not quests:
                self.status = 'Aguardando missões e objetivos disponíveis'
                return None
            quest = min(quests, key=lambda q: (
                {'ready': 0, 'active': 1, 'available': 2}[q.state],
                q.target.distance, q.identity))
            target = quest.target
            if quest.state == 'active' and quest.objective not in ('gather', 'interact'):
                self.status = 'Objetivo depende de integração de combate ou outra mecânica'
                return None
            operation = {'ready': 'turn_in_quest', 'available': 'accept_quest',
                         'active': quest.objective}[quest.state]
            argument = (quest.identity, target.identity)
        if target.distance > target.interaction_range:
            operation, argument = 'navigate', target.identity
            self.navigation_target = target.identity
            if self.navigation_progress is None or self.navigation_progress[0]!=target.identity:
                self.navigation_progress=(target.identity,target.distance,now)
        else:
            self.navigation_target = None
            self.navigation_progress=None
        self.pending = Command(operation, argument)
        self._last_command, self._before = self.pending, obs
        self._revision, self._observed_at = obs.revision, obs.observed_at
        self._issued_at = now
        self.status = f'{operation}: {target.name}'
        return self.pending
