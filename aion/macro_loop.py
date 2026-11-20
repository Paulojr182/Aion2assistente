"""Combat loop for the game's own combo macro; no routes or desktop input."""
from dataclasses import dataclass
import math


class SnapshotUnavailable(RuntimeError):pass
class TargetChanged(RuntimeError):pass
class MacroNotReady(RuntimeError):pass


@dataclass(frozen=True)
class MacroTarget:
    identity: tuple[int,int]
    health: int
    maximum: int
    eligible: bool
    label: str='Inimigo'


@dataclass(frozen=True)
class MacroSnapshot:
    scope: str
    captured_at: float
    player_health: int
    player_maximum: int
    macro_active: bool
    target: MacroTarget | None
    defeated: tuple[tuple[int,int],...]=()
    player_identity: tuple[int,int] | None=None
    input_identity: tuple[int,int] | None=None


class MacroLoop:
    def __init__(self,backend,*,search_interval=0.3,max_age=0.75,center=None,radius=None,names=()):
        self.backend=backend
        self.search_interval=search_interval
        self.max_age=max_age
        self.center=tuple(center) if center is not None else None
        self.radius=float(radius) if radius is not None else None
        self.names=frozenset(names) if names else frozenset()
        self.scope=None
        self.target=None
        self.next_search=-math.inf
        self.was_running=False
        self.next_macro_try=-math.inf
        self.target_missing_since=None
        self.combo=()               # vazio = macro do jogo (padrao). Senao: ((slot 1..8, espera_s), ...)
        self.combo_index=0;self.next_combo_at=-math.inf
    def _combo_step(self,snapshot,target,now):
        if now>=self.next_combo_at:
            slot,wait=self.combo[self.combo_index%len(self.combo)]
            try:
                self.backend.skill_press(snapshot,slot)
            except TargetChanged:
                return {'state':'searching','message':'Atualizando o próximo inimigo.'}
            except RuntimeError as exc:
                self.next_combo_at=now+0.5
                return {'state':'waiting','message':f'Skill {slot} indisponível: {exc}'}
            self.combo_index=(self.combo_index+1)%len(self.combo)
            self.next_combo_at=now+max(0.1,wait)
        return {'state':'attacking','message':f'Combo de skills em {target.label} — HP {target.health:,}/{target.maximum:,}'.replace(',','.'),
                'target_health':target.health,'target_maximum':target.maximum}
    def step(self,snapshot,*,running,now):
        if not running:
            self.backend.stop_macro(force=self.was_running)
            self.was_running=False;self.target=None;self.target_missing_since=None
            return {'state':'stopped','message':'Ataque automático parado.'}
        if not self.was_running:
            # Ataque: o raio e medido a partir de onde o play foi dado (sem centro salvo).
            self.center=None
            try:
                pos=self.backend.reader.flight_state().get('position',{}).get('position')
                if pos: self.center=tuple(pos)
            except Exception:
                pass
        self.was_running=True
        if (not snapshot.scope or not math.isfinite(snapshot.captured_at)
            or not 0<=now-snapshot.captured_at<=self.max_age):
            self.backend.stop_macro(force=True)
            raise SnapshotUnavailable('Leitura do jogo ficou indisponível ou desatualizada.')
        if self.scope!=snapshot.scope:
            self.backend.stop_macro(force=True)
            self.scope=snapshot.scope;self.target=None;self.next_search=-math.inf
            self.target_missing_since=None
        if snapshot.player_health<=0 or snapshot.player_maximum<=0:
            self.backend.stop_macro(force=True);self.target=None
            return {'state':'waiting','message':'Aguardando o personagem estar pronto.'}
        target=snapshot.target
        if target is not None and self.names:
            def matches_target(n, tgt):
                low_n = n.lower().strip()
                low_lbl = tgt.label.lower().strip()
                if low_n in low_lbl or low_lbl in low_n:
                    return True
                base_n = low_n.split('(')[0].strip()
                base_lbl = low_lbl.split('(')[0].split('nível')[0].strip()
                if base_n and (base_n in low_lbl or base_lbl in low_n):
                    return True
                if low_lbl.startswith('inimigo') or low_lbl.startswith('alvo') or low_lbl.startswith('boss'):
                    for part in low_n.split():
                        if part.isdigit() and part in low_lbl:
                            return True
                    return True
                return False
            if not any(matches_target(n, target) for n in self.names):
                self.backend.stop_macro(force=True)
                self.target=None
                if now>=self.next_search:          # ja aperta Tab para o proximo inimigo
                    self.next_search=now+self.search_interval
                    try: self.backend.next_target(snapshot)
                    except TargetChanged: pass
                return {'state':'searching','message':f'Ignorando {target.label} (não está na lista de alvos).'}
        if (self.center is not None and self.radius is not None and hasattr(self.backend, 'reader')
                and now>=getattr(self,'_next_radius_check',-math.inf)):
            self._next_radius_check=now+1.0   # leitura de posicao e cara; 1x por segundo basta
            try:
                state=self.backend.reader.flight_state()
                pos=state.get('position',{}).get('position')
                if pos and math.dist(pos[:2],self.center[:2])>self.radius:
                    self.backend.stop_macro(force=True)
                    self.target=None
                    return {'state':'waiting','message':'Personagem fora do raio de farm configurado.'}
            except Exception:
                pass
        if target is None or not target.eligible or target.health<=0 or target.maximum<=0:
            self.backend.stop_macro(force=True)
            confirmed_dead=self.target in snapshot.defeated or (
                target is not None and target.identity==self.target and target.health<=0
            )
            if confirmed_dead:
                self.next_search=-math.inf
            if self.target is not None and not confirmed_dead:
                if self.target_missing_since is None:
                    self.target_missing_since=now
                if now-self.target_missing_since<0.4:
                    return {'state':'waiting','message':'Confirmando o alvo atual.'}
            self.target=None
            self.target_missing_since=None
            if now>=self.next_search:
                self.next_search=now+self.search_interval
                try:
                    self.backend.next_target(snapshot)
                except TargetChanged:
                    return {'state':'waiting','message':'Confirmando o alvo atual.'}
            return {'state':'searching','message':'Procurando um inimigo próximo.'}
        self.target_missing_since=None
        if self.target!=target.identity:
            self.backend.stop_macro(force=True)
            self.target=target.identity;self.next_macro_try=-math.inf;self.combo_index=0
        if self.combo:
            return self._combo_step(snapshot,target,now)
        if now<self.next_macro_try:
            return {'state':'waiting','message':'Aguardando o macro ficar disponível.'}
        try:
            if not self.backend.ensure_macro(snapshot):
                raise RuntimeError('O jogo não confirmou a ativação do macro.')
        except TargetChanged:
            self.backend.stop_macro(force=True)
            return {'state':'searching','message':'Atualizando o próximo inimigo.'}
        except MacroNotReady:
            self.next_macro_try=now+0.3
            return {'state':'waiting','message':'Aguardando o macro ficar disponível.'}
        return {'state':'attacking','message':f'Atacando {target.label} — HP {target.health:,}/{target.maximum:,}'.replace(',','.'),
                'target_health':target.health,'target_maximum':target.maximum}

    def close(self):
        self.backend.stop_macro(force=self.was_running)
        self.was_running=False;self.target=None;self.target_missing_since=None
