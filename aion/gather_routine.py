"""Rotina de coleta como jogador.

Regras (pedidas pelo usuário):
1. Indo até o spot: assim que o botão "F Coleta" aparecer, para na hora (não precisa ficar em cima).
2. Coleta: aperta F uma vez e espera a coleta terminar; se o F aparecer de novo, repete.
   Continua até o F parar de aparecer; só então segue para o próximo spot.
3. Autodefesa: se um mob atacar (HP caiu), para tudo, mata SÓ esse mob
   com o ataque automático (macro) e volta para a coleta, sem procurar outros inimigos.
"""
import math
import time

from .gather_identity import format_gather_identity
from .route_nav import FLIGHT_MIN_STAMINA, AERIAL_HEIGHT
from .macro_loop import SnapshotUnavailable
from .native_player import PlayerObservationError
TRANSIENT = (SnapshotUnavailable, PlayerObservationError)

PROGRESS_IDLE = ('hud_absent', 'progress_absent', 'progress_hidden', 'player_absent')


class GatherRoutine:
    def __init__(self, nav, transport, reader, *, log=print, stop=lambda: False,
                 max_presses=12, press_timeout=15.0, fight_timeout=60.0):
        self.nav, self.transport, self.reader = nav, transport, reader
        self.log, self.stop = log, stop
        self.max_presses, self.press_timeout, self.fight_timeout = max_presses, press_timeout, fight_timeout
        self.last_health = None
        self.done = {}            # identidade do spot -> horário em que terminou
        self.stats = {'spots': 0, 'presses': 0, 'fights': 0}
        self.needs_reapproach = False
        self.position_reset = False
        self.approach_failures = {}
        self.debug = lambda kind, value: None
        self.radius = None        # raio maximo (horizontal) a partir de onde a rotina comecou; None = sem limite
        self.anchor = None
        self._low_stamina_logged = False
        self.names = None          # nomes amigaveis permitidos (painel); None = todos
        self.ground_only = False   # True: nunca voa, ignora spots no alto
        self.waypoints = []        # pontos de rota: sem spot no raio, vai para o proximo
        self._wp_index = -1
        self.min_stamina = FLIGHT_MIN_STAMINA

    # --- percepção ------------------------------------------------------------------------
    def prompt(self):
        """Botão 'F Coleta' visível e válido agora (mesma leitura validada do despacho)."""
        try:
            c = self.reader.contextual()
        except Exception as exc:
            self.debug('prompt_erro', f'{type(exc).__name__}: {exc}'[:160])
            return False
        self.debug('prompt', {k: c.get(k) for k in ('prompt_verified', 'dispatch_eligible', 'resolved_action', 'selection_enabled')})
        return bool(c.get('prompt_verified') and c.get('dispatch_eligible')
                    and c.get('resolved_action') in (197, 200) and c.get('selection_enabled'))

    def attacked(self, snapshot):
        """Só é ataque quando o HP do personagem CAI. Um alvo apenas selecionado não conta."""
        dropped = self.last_health is not None and snapshot.player_health < self.last_health
        self.debug('ataque', {'hp': snapshot.player_health, 'antes': self.last_health,
                              'alvo': None if snapshot.target is None else [snapshot.target.label, snapshot.target.health, snapshot.target.eligible]})
        self.last_health = snapshot.player_health
        return dropped

    def progress_stage(self):
        try:
            return self.reader.gather_progress().stage
        except Exception:
            return None

    # --- autodefesa -----------------------------------------------------------------------
    def defend(self):
        """Mata um único mob que está atacando, com o macro de combate; não procura outros."""
        self.stats['fights'] += 1
        try: self.nav.set_running(False)
        except Exception: pass
        snapshot = self.transport.snapshot()
        if snapshot.target is None or not snapshot.target.eligible or snapshot.target.health <= 0:
            try:
                self.transport.next_target(snapshot)   # Tab: pega quem está batendo
            except Exception as exc:
                self.log(f'Autodefesa: não consegui selecionar o atacante ({exc}).')
            time.sleep(0.3)
            snapshot = self.transport.snapshot()
        target = snapshot.target
        if target is None or not target.eligible or target.health <= 0:
            self.log('Autodefesa: nenhum mob hostil para combater; voltando à coleta.')
            self.last_health = snapshot.player_health
            return
        identity = target.identity
        self.log(f'Autodefesa: combatendo {target.label} (HP {target.health}/{target.maximum}).')
        deadline = time.monotonic() + self.fight_timeout
        try:
            while time.monotonic() < deadline and not self.stop():
                snapshot = self.transport.snapshot()
                t = snapshot.target
                if t is None or t.identity != identity or t.health <= 0:
                    self.log('Autodefesa: mob eliminado.'); break
                if snapshot.player_health <= 0:
                    self.log('Personagem morreu.'); break
                try:
                    self.transport.ensure_macro(snapshot)
                except Exception as exc:   # macro ainda não liberado, alvo trocou etc.: tenta de novo
                    self.log(f'Autodefesa: {exc}')
                time.sleep(0.25)
        finally:
            try: self.transport.stop_macro(force=True)
            except Exception as exc: self.log(f'Falha ao parar o macro: {exc}')
        time.sleep(0.5)
        self.last_health = self.transport.snapshot().player_health

    # --- coleta no spot -------------------------------------------------------------------
    def collect_here(self, goal=None):
        """Aperta F enquanto o botão aparecer, esperando cada coleta terminar."""
        presses = 0
        self.needs_reapproach = False
        precise = goal is not None and getattr(self.nav, 'flight', False)
        if precise and not self.nav.settle_for_collection(goal,reach=450.0) and not self.prompt():
            if math.dist(self.nav.position(),goal)>900.0:
                self.position_reset=True
                self.log('Retorno grande de posicao apos aproximacao; rotina encerrada.')
            self.log('F visivel, mas parada/alcance ainda nao confirmados; reaproximando antes de coletar.')
            self.needs_reapproach = True
            return 0
        while presses < self.max_presses and not self.stop():
            snapshot = self.transport.snapshot()
            if self.attacked(snapshot):
                self.defend(); continue
            if not self.prompt():
                time.sleep(0.4)
                if not self.prompt():
                    break                      # F não aparece mais: spot esgotado
            current_distance = math.dist(self.nav.position(),goal) if precise else 0.0
            if precise and current_distance>900.0:
                self.position_reset=True
                self.log('Retorno grande de posicao antes da coleta; nenhuma nova tentativa de voo.')
                return presses
            if precise and current_distance>450.0:
                self.needs_reapproach = True
                self.log('Saiu do alcance apos parar; reaproximando sem pressionar F.')
                return presses
            try:
                self.transport.collect(snapshot)
            except Exception as exc:
                self.log(f'F recusado: {exc}'); time.sleep(0.5); continue
            presses += 1; self.stats['presses'] += 1
            self.log(f'F pressionado ({presses}). Aguardando a coleta terminar...')
            if not self.wait_collection():
                if not self.stop():
                    self.needs_reapproach = True
                    self.log('Coleta nao iniciou/terminou pelo HUD; nova aproximacao antes de repetir F.')
                return presses
        return presses

    def wait_collection(self):
        """Espera a barra de coleta aparecer e sumir (ou o prazo)."""
        started = time.monotonic(); seen = False; idle_reads = 0
        while time.monotonic() - started < self.press_timeout and not self.stop():
            snapshot = self.transport.snapshot()
            if self.attacked(snapshot):
                self.log('Atacado durante a coleta.'); self.defend(); return False
            stage = self.progress_stage()
            if stage == 'progress_visible':
                seen = True; idle_reads = 0
            elif stage in PROGRESS_IDLE:
                idle_reads += 1
                if (seen and idle_reads >= 2) or (not seen and time.monotonic() - started > 3.0):
                    time.sleep(0.6)
                    return seen
            time.sleep(0.25)
        return False

    # --- escolha de spots e laço ----------------------------------------------------------
    def next_spot(self, exclude_seconds=90.0):
        report = self.reader.gather_sources()
        pos = self.nav.position()
        now = time.monotonic()
        rows = []
        from .friendly_names import resolve_gather_name
        why = {}
        cooled = []   # spots tentados ha pouco; usados se nao houver outro
        def skip(reason, name):
            why.setdefault(reason, set()).add(name)
        for r in report.get('targets', ()):
            name = resolve_gather_name(r.get('source', {}).get('name', ''))
            if self.names and name not in self.names:
                continue
            if not r.get('mastery_requirement_met'):
                skip('sem maestria', name); continue
            if not r.get('position', {}).get('position_observed'):
                skip('sem posicao lida', name); continue
            if self.radius and self.anchor is not None and math.dist(r['position']['position'][:2], self.anchor[:2]) > self.radius:
                skip('fora do raio', name); continue
            ident = format_gather_identity(r)
            age = now - self.done.get(ident, -1e9)
            if age < exclude_seconds:
                if age >= 15.0:
                    cooled.append((math.dist(r['position']['position'], pos), ident, r))
                skip('coletado ha pouco', name); continue
            rows.append((math.dist(r['position']['position'], pos), ident, r))
        if not rows and cooled:
            rows = cooled
        self.last_skip = '; '.join(f"{k}: {', '.join(sorted(v))}" for k, v in why.items())
        if not rows:
            try:
                import json
                from pathlib import Path
                dump = {'player': list(pos) if pos else None, 'anchor': list(self.anchor) if self.anchor else None,
                        'radius': self.radius, 'names': sorted(self.names) if self.names else None,
                        'report_keys': {k: v for k, v in report.items() if k != 'targets' and isinstance(v, (str, int, float, bool, type(None)))},
                        'targets': [{'friendly': resolve_gather_name(r.get('source', {}).get('name', '')),
                                     'raw': r.get('source', {}).get('name'),
                                     'distance': r.get('distance_engine_units'),
                                     'mastery': r.get('mastery_requirement_met'),
                                     'observed': r.get('position', {}).get('position_observed'),
                                     'position': r.get('position', {}).get('position')} for r in report.get('targets', ())]}
                Path(__file__).resolve().parent.parent.joinpath('data', 'gather_debug.json').write_text(
                    json.dumps(dump, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
            except Exception:
                pass
            return None
        rows.sort(key=lambda x: x[0])
        if self.ground_only:
            rows = [x for x in rows if abs(x[2]['position']['position'][2] - pos[2]) <= AERIAL_HEIGHT]
            if not rows:
                self.last_skip = (self.last_skip + '; ' if self.last_skip else '') + 'spots no alto (modo so chao)'
                return None
            return rows[0]
        if getattr(self.nav, 'flight', False) and not self.can_fly():
            ground = [x for x in rows if abs(x[2]['position']['position'][2] - pos[2]) <= AERIAL_HEIGHT]
            if len(ground) < len(rows) and not self._low_stamina_logged:
                self.log(f'Estamina de voo abaixo de {self.min_stamina:.0%}: ignorando spots no alto, procurando no chao.')
                self._low_stamina_logged = True
            if not ground:
                return None
            return ground[0]
        self._low_stamina_logged = False
        return rows[0]

    def can_fly(self):
        """Ja no ar, ou estamina de voo >= 70%."""
        try:
            if self.nav._flight_state()['movement']['MovementMode'] == 5: return True
            ratio = self.reader.flight_stamina_ratio()
            return ratio is not None and math.isfinite(ratio) and ratio >= self.min_stamina
        except Exception:
            return False

    def goto_next_waypoint(self):
        """Vai ao proximo ponto de rota; o raio passa a ser medido a partir dele."""
        self._wp_index = (self._wp_index + 1) % len(self.waypoints)
        goal = tuple(self.waypoints[self._wp_index])
        self.anchor = goal
        self.log(f'Sem spot aqui; indo para o ponto {self._wp_index + 1} de {len(self.waypoints)}.')
        def interrupt():
            if self.stop(): return 'cancelado'
            try:
                if self.attacked(self.transport.snapshot()): return 'atacado'
            except TRANSIENT:
                pass
            return None
        for _ in range(3):
            if self.stop(): return False
            try:
                result = self.nav.go(goal, reach=1500.0, interrupt=interrupt)
            except TRANSIENT:
                time.sleep(0.5); continue
            except RuntimeError as exc:
                self.log(f'Falha indo ao ponto ({exc}).')
                release = getattr(self.nav, 'release_all', None)
                if release: release()
                break
            if result == 'atacado':
                self.defend(); continue
            break
        time.sleep(1.5)        # deixa o jogo carregar os spots da regiao
        return True

    def run(self, max_spots=None, radius=None, center=None):
        self.last_health = self.transport.snapshot().player_health
        self.radius = radius; self.anchor = tuple(center) if center else tuple(self.nav.position())
        self.nav.min_stamina = self.min_stamina
        if radius: self.log(f'Farm no raio de {radius / 100:.0f} m; spot mais proximo (chao ou alto); voo com estamina >= {self.min_stamina:.0%}.')
        waiting = False
        while not self.stop() and (max_spots is None or self.stats['spots'] < max_spots):
            refresh = getattr(self, 'refresh_names', None)
            if refresh is not None:
                try: self.names = refresh()
                except Exception: pass
            try:
                pick = self.next_spot()
            except TRANSIENT as exc:
                self.debug('leitura_transitoria', str(exc)[:120]); time.sleep(0.5); continue
            if pick is None:
                if not self.names and self.prompt():                      # F na tela = tem spot aqui, mesmo fora da lista
                    self.log('F visivel aqui; coletando.')
                    presses = self.collect_here()
                    if presses: self.stats['spots'] += 1
                    continue
                if self.waypoints and self.goto_next_waypoint():
                    waiting = False; continue
                skip = getattr(self, 'last_skip', '')
                if not waiting or skip != getattr(self, '_shown_skip', None):
                    self._shown_skip = skip
                    self.log('Nenhum spot no raio. Aguardando nascer...' + (f' (descartados: {skip})' if skip else ' (nenhum spot selecionado visivel)'))
                    land = getattr(self.nav, 'land', None)
                    if land and not waiting: land()
                waiting = True
                if self.attacked(self.transport.snapshot()): self.defend()
                time.sleep(3); continue
            waiting = False
            dist, ident, row = pick
            goal = tuple(row['position']['position'])
            self.log(f"Indo para {row['source']['name']} ({dist:.0f} de distância).")

            def interrupt():
                if self.stop(): return 'cancelado'
                try:
                    snapshot = self.transport.snapshot()
                    if self.attacked(snapshot): return 'atacado'
                    prompt_reach = 450.0   # F visivel ja confirma o alcance (chao e voo)
                    if math.dist(self.nav.position(), goal) < prompt_reach and self.prompt(): return 'F apareceu'
                except TRANSIENT as exc:            # leitura pegou o jogo no meio de uma atualizacao
                    self.debug('leitura_transitoria', str(exc)[:120])
                return None

            transient = 0
            while not self.stop():
                try:
                    result = self.nav.go(goal, reach=110.0, interrupt=interrupt)
                except TRANSIENT as exc:
                    transient += 1
                    self.debug('leitura_transitoria', str(exc)[:120])
                    if transient > 5:
                        self.log('Leituras falhando seguidamente; pulando esse spot.'); result = 'leitura instavel'; break
                    for fn in (getattr(self.nav, 'release_all', None), lambda: self.nav.set_running(False)):
                        try:
                            if fn: fn()
                        except Exception: pass
                    time.sleep(0.5); continue
                except RuntimeError as exc:
                    self.log(f'Falha na navegacao ({exc}); soltando comandos e pulando esse spot.')
                    release = getattr(self.nav, 'release_all', None)
                    if release: release()
                    result = 'falha navegacao'; break
                self.debug('nav_resultado', result)
                if result == 'atacado':
                    self.defend(); continue    # volta a ir para o mesmo spot
                if result == 'aguardando estamina':
                    self.log('Sem estamina para esse spot no alto; indo para outro.')
                    self.done[ident] = time.monotonic() - 60.0   # volta a considerar em ~30 s
                    break
                if result == 'sem montaria':
                    self.log('Aguardando poder montar no pet...'); time.sleep(3); continue
                break
            if self.stop(): break
            if result == 'aguardando estamina':
                continue                                   # ja marcado para rever em ~30 s
            if result in ('voo sem progresso', 'voo interrompido'):
                self.log('Voo nao chegou nesse spot; pulando para o proximo.')
                self.done[ident] = time.monotonic(); continue
            if result not in ('F apareceu', True):
                self.log('Não consegui chegar a esse spot; tentando outro.')
                self.done[ident] = time.monotonic(); continue
            prepare = getattr(self.nav, 'prepare_collect', None)
            if prepare is not None and not prepare() and not self.prompt():
                self.done[ident] = time.monotonic()
                continue
            presses = self.collect_here(goal=goal)
            if self.position_reset:
                return self.stats
            if self.needs_reapproach or presses == 0:
                failures = self.approach_failures.get(ident,0)+1
                self.approach_failures[ident] = failures
                if failures >= 3:
                    self.log('Coleta nao confirmada apos tres aproximacoes; pulando esse spot.')
                    self.approach_failures.pop(ident, None)
                    self.done[ident] = time.monotonic()
                continue
            self.approach_failures.pop(ident,None)
            self.log(f'Spot concluído ({presses} coleta(s)).')
            self.done[ident] = time.monotonic(); self.stats['spots'] += 1
        return self.stats
