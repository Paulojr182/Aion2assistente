"""Independent target/combat policy. No dependency on the old executable."""
from dataclasses import dataclass
from enum import Enum
import math


class Mode(str, Enum):
    AUTO_ATTACK = 'Ataque automático'
    SEMI_MANUAL = 'Semi manual'
    FARM = 'Auto farm'


@dataclass(frozen=True)
class Entity:
    id: int
    actor: int
    name: str
    distance: float
    hp: int
    hostile: bool = True
    targetable: bool = True
    hidden: bool = False
    kind: str = 'monster'
    position: tuple[float, float, float] | None = None

    @property
    def eligible(self):
        return (self.id > 0 and self.actor > 0 and self.hp > 0 and self.hostile
                and self.targetable and not self.hidden and self.kind in ('monster', 'boss')
                and math.isfinite(self.distance) and self.distance >= 0)


@dataclass(frozen=True)
class Skill:
    key: str
    cooldown: float
    max_range: float

    def __post_init__(self):
        if not self.key.strip() or not math.isfinite(self.cooldown) or self.cooldown < 0:
            raise ValueError('Tecla ou intervalo de habilidade inválido.')
        if not math.isfinite(self.max_range) or self.max_range <= 0:
            raise ValueError('Alcance de habilidade inválido.')


@dataclass(frozen=True)
class Observation:
    session: str
    observed_at: float
    entities: tuple[Entity, ...]
    selected: tuple[int, int] | None
    connected: bool = True
    foreground: bool = True
    player_alive: bool = True
    ready_skills: frozenset[str] = frozenset()
    mana_sufficient: bool = True
    casting: bool = False
    background_input_ready: bool = False

    @property
    def input_ready(self):
        return self.foreground is True or self.background_input_ready is True


@dataclass(frozen=True)
class Command:
    operation: str
    argument: object = None


class CombatPolicy:
    """Plans one acknowledged action at a time; the provider owns game IO.

    set_macro means an acknowledged desired-state change, never a repeated key.
    Macro knowledge is retained across errors until a stop is acknowledged.
    No routes, farm center or saved coordinates exist in auto-attack mode.
    """

    def __init__(self, mode=Mode.AUTO_ATTACK, skills=(), max_age=1.0,
                 selected_names=(), macro_key='ç', center=None, radius=None):
        if not math.isfinite(max_age) or max_age <= 0:
            raise ValueError('Validade da observação inválida.')
        if not macro_key.strip():
            raise ValueError('Configure a tecla do macro.')
        if radius is not None and (type(radius) not in (int, float) or not math.isfinite(radius) or radius <= 0):
            raise ValueError('Raio inválido.')
        if center is not None and (not isinstance(center, (tuple, list)) or len(center) not in (2, 3) or not all(type(v) in (int, float) and math.isfinite(v) for v in center)):
            raise ValueError('Centro inválido.')
        self.mode = Mode(mode)
        self.skills = tuple(skills)
        if len({s.key for s in self.skills}) != len(self.skills):
            raise ValueError('Habilidades duplicadas.')
        self.max_age = max_age
        self.names = frozenset(selected_names)
        self.macro_key = macro_key
        self.center = tuple(center) if center is not None else None
        self.radius = float(radius) if radius is not None else None
        self.running = False
        self.macro_on = False
        self.session = None
        self.pending = None
        self.last_skill = {}
        self.selection_attempts = 0
        self.last_selection = -math.inf
        self.status = 'Parado'

    def start(self):
        if self.pending or self.macro_on:
            raise RuntimeError('Confirme a parada do macro antes de reiniciar.')
        if self.running:
            raise RuntimeError('Uma rotina já está em execução.')
        self.running = True
        self.session = None
        self.last_skill.clear()
        self.selection_attempts = 0
        self.last_selection = -math.inf
        self.status = 'Aguardando leitura do jogo'

    def stop(self):
        self.running = False
        self.status = 'Parando macro' if self.macro_on else 'Parado'

    def _issue(self, operation, argument=None):
        self.pending = Command(operation, argument)
        return self.pending

    def acknowledge(self, command, succeeded, now):
        if self.pending != command:
            raise RuntimeError('Confirmação não corresponde ao comando pendente.')
        self.pending = None
        if not succeeded:
            self.running = False
            self.status = 'Falha no comando; confirme o estado do macro no jogo'
            # A failed toggle may have reached the game. Do not guess its state.
            if command.operation == 'set_macro':
                self.pending = Command('verify_macro_state')
            return
        if command.operation == 'set_macro':
            self.macro_on = command.argument
        elif command.operation == 'cast':
            self.last_skill[command.argument] = now
        elif command.operation == 'select_target':
            self.last_selection = now
            self.selection_attempts += 1

    def confirm_macro_state(self, enabled):
        if self.pending != Command('verify_macro_state') or type(enabled) is not bool:
            raise RuntimeError('Não existe verificação de macro pendente.')
        self.macro_on = enabled
        self.pending = None

    def step(self, obs, now):
        if self.pending:
            return None
        available = (obs.connected and obs.input_ready and obs.session
                     and 0 <= now - obs.observed_at <= self.max_age)
        if self.session is not None and obs.session != self.session:
            self.running = False
            self.status = 'Sessão mudou; confirme a parada do macro na sessão anterior'
            # Never send the old session's toggle to a new process/session.
            return None
        if not available:
            self.status = 'Aguardando canal de entrada disponível e leitura atualizada'
            return None
        if self.session is None:
            self.session = obs.session
        if not self.running or not obs.player_alive:
            self.running = False
            self.status = 'Parando macro' if self.macro_on else 'Parado'
            return self._issue('set_macro', False) if self.macro_on else None
        candidates = [e for e in obs.entities if e.eligible and (not self.names or e.name in self.names)]
        if self.center is not None and self.radius is not None:
            def in_radius(e):
                if e.position is not None:
                    return math.dist(e.position[:2], self.center[:2]) <= self.radius
                return e.distance <= self.radius
            candidates = [e for e in candidates if in_radius(e)]
        selected = next((e for e in candidates if (e.id, e.actor) == obs.selected), None)
        if selected is None:
            if self.macro_on:
                self.status = 'Desligando macro antes de trocar o alvo'
                return self._issue('set_macro', False)
            if self.mode == Mode.SEMI_MANUAL:
                self.status = 'Selecione um alvo válido no jogo'
                return None
            if not candidates:
                self.status = 'Aguardando inimigos próximos'
                return None
            if now - self.last_selection < 1.0:
                self.status = 'Aguardando confirmação da seleção'
                return None
            if self.selection_attempts >= 3:
                self.running = False
                self.status = 'O jogo não confirmou a seleção após três tentativas'
                return None
            target = min(candidates, key=lambda e: (e.distance, e.id, e.actor))
            self.status = f'Selecionando {target.name}'
            return self._issue('select_target', (target.id, target.actor))
        self.selection_attempts = 0
        if not self.macro_on:
            self.status = f'Ativando macro em {selected.name}'
            return self._issue('set_macro', True)
        self.status = f'Atacando {selected.name}'
        if obs.casting or not obs.mana_sufficient:
            return None
        for skill in self.skills:
            if (skill.key in obs.ready_skills and selected.distance <= skill.max_range
                    and now - self.last_skill.get(skill.key, -math.inf) >= skill.cooldown):
                return self._issue('cast', skill.key)
        return None
