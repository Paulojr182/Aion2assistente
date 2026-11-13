"""Navega por pedidos posicionais (estado 2), como o clique em terreno vazio.
A parada usa a rotina de cancelamento do personagem, sem iniciar uma rota de IA.
"""
import math
import struct
import time

FLIGHT_MIN_STAMINA = 0.70          # so sobe para spot aereo com estamina de voo >= 70%
AERIAL_HEIGHT = 300.0              # diferenca de altura que exige voo
MOUNTED_MIN_WALK_SPEED = 700.0    # medido no jogo: a pé 519, montado 902


class RouteNavigator:
    def __init__(self, transport, reader, *, log=print, ride=False, flight=False):
        from aion.native_player import ObjectRegistry, observe_player
        from aion.reflection_fields import ReflectionSchema
        self.transport, self.reader, self.log, self.ride = transport, reader, log, ride
        with reader.process(4194304) as process:
            pawn = observe_player(process, reader.roots)['roles']['player']['address']
            registry = ObjectRegistry(process, reader.roots)
            schema = ReflectionSchema(registry, max_bytes=2097152)
            cmc = schema.linked(registry.object(pawn), 'Character', 'CharacterMovement')
            field = schema.field(cmc, 'CharacterMovementComponent', 'MaxWalkSpeed')
            self.cmc, self.speed_offset = cmc['address'], field.offset
        self.flight_preparation = []
        self.send_receipts = []
        self.flight = flight
        self.trace = []

    def position(self):
        return self._flight_state()['position']['position']

    def _flight_state(self, **kw):
        """flight_state com novas tentativas quando a leitura pega o jogo no meio de uma atualizacao."""
        from aion.macro_loop import SnapshotUnavailable
        from aion.native_player import PlayerObservationError
        for attempt in range(8):
            try:
                return self.reader.flight_state(**kw)
            except (SnapshotUnavailable, PlayerObservationError):
                if attempt == 7: raise
                time.sleep(0.03)

    def _release(self, button):
        """Solta um botao (FlyUp/FlyDown) insistindo; nunca deixa o botao preso."""
        for attempt in range(6):
            try:
                self.transport.button_action(self.transport.snapshot(), button, release=True)
                return True
            except Exception as exc:
                self.log(f'Falha ao soltar {button} (tentativa {attempt + 1}): {exc}')
                time.sleep(0.1)
        return False

    def land(self):
        """Sem spot para coletar e ainda no ar: aperta V (Move_Fly) para pousar."""
        try:
            if self._flight_state()['movement']['MovementMode'] != 5: return True
            self.release_all()
            self.log('Sem spot no raio e no ar: pousando (V).')
            return self.transport.flight_land(self.transport.snapshot())
        except Exception as exc:
            self.log(f'Nao consegui pousar: {exc}')
            return False

    def release_all(self):
        """Seguranca: solta subir/descer e desliga a corrida do ajuste final."""
        for button in ('Move_FlyUp', 'Move_FlyDown'):
            self._release(button)
        if getattr(self, '_running_final', False):
            try: self.transport.autorun_toggle(self.transport.snapshot()); self._running_final = False
            except Exception as exc: self.log(f'Falha ao desligar corrida: {exc}')

    def max_walk_speed(self):
        with self.reader.process(1048576) as process:
            return struct.unpack('<f', process.read(self.cmc + self.speed_offset, 4))[0]

    def mounted(self):
        return self.max_walk_speed() >= MOUNTED_MIN_WALK_SPEED

    def mount(self, timeout=15.0):
        """Monta no pet antes de andar. Nunca anda a pé (deslizaria): tenta de novo até conseguir.
        Retorna True se montado."""
        if not self.ride: return True
        if self.mounted(): return True
        deadline = time.monotonic() + timeout; attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            speed = self.max_walk_speed()
            self.transport.button_action(self.transport.snapshot(), 'Move_Ride')
            self.log(f'Montando no pet (tentativa {attempt}, velocidade a pé {speed:.0f}).')
            wait_until = time.monotonic() + 3.0
            while time.monotonic() < wait_until:
                time.sleep(0.3)
                if self.mounted():
                    self.log(f'Montado (velocidade {self.max_walk_speed():.0f}).'); return True
            time.sleep(2.0)   # ex.: logo após combate o jogo ainda não deixa montar
            if self.mounted(): return True
        self.log('Não consegui montar no pet; não vou andar a pé (deslizaria).')
        return False

    def prepare_collect(self, timeout=6.0):
        """Desmonta antes do F quando a montaria ainda estiver ativa. Em voo nao ha montaria."""
        try:
            if self._flight_state()['movement']['MovementMode'] == 5: return True
        except Exception: pass
        if not self.mounted(): return True
        self.transport.button_action(self.transport.snapshot(), 'Move_Ride')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.25)
            if not self.mounted(): return True
        self.log('Nao consegui desmontar; coleta adiada.')
        return False

    def stop(self):
        """Encerra o deslocamento do jogo mandando a rota para a posição atual."""
        self.transport.auto_move(self.transport.snapshot(), (0.0, 0.0, 0.0), execute=True, map_stop=True)

    # compatibilidade com a rotina de coleta
    def set_running(self, on):
        if not on: self.stop()

    def flight_telemetry(self):
        from aion.native_player import RegistryChanged
        for attempt in range(3):
            try:
                return self._flight_telemetry_once()
            except RegistryChanged:
                if attempt == 2:
                    raise
                time.sleep(0.02)

    def _flight_telemetry_once(self):
        from aion.native_player import observe_player
        with self.reader.process(2097152) as process:
            pawn = observe_player(process, self.reader.roots)['roles']['player']['address']
            movement = struct.unpack('<Q', process.read(pawn + 968, 8))[0]
            raw = process.read(pawn + 0x1760, 0x1E0)
            modes = list(process.read(movement + 545, 2))
        return {'movement_modes': modes, 'auto_state': raw[0],
                'request_flags': list(raw[0x120:0x123]), 'pawn_1760': raw.hex()}

    def wait_flight_ready(self, timeout=4.0):
        """Espera a transicao de decolagem terminar, sem reenviar Move_Fly."""
        deadline = time.monotonic() + timeout
        stable = 0
        expected = None
        while time.monotonic() < deadline:
            state = self._flight_state(with_auto_state=True)
            identity = (state['session'], tuple(state['player_identity']))
            if expected is None:
                expected = identity
            elif expected != identity:
                raise RuntimeError('Personagem/sessao mudou durante decolagem.')
            sample = {'t': round(time.monotonic(), 3),
                      'movement_mode': state['movement']['MovementMode'],
                      'auto_state': state['auto_move_state']}
            if not hasattr(self, 'flight_preparation'):
                self.flight_preparation = []
            self.flight_preparation.append(sample)
            stable = stable + 1 if sample['movement_mode'] == 5 and sample['auto_state'] == 0 else 0
            if stable >= 3:
                return
            time.sleep(0.1)
        raise RuntimeError('Decolagem nao estabilizou: destino nao enviado e decolagem nao repetida.')

    def send_goal(self, goal):
        """Caminhada aprovada; voo usa controle 3D separado."""
        reply = self.transport.auto_move(self.transport.snapshot(),goal,execute=True,map_position=True)
        if not hasattr(self, 'send_receipts'):
            self.send_receipts = []
        self.send_receipts.append({'goal':goal,'aerial':False,'reply':reply})
        return True

    def settle_for_collection(self, goal, *, reach=180.0, timeout=3.0):
        """Nao confunde F visivel com alcance apos o personagem parar."""
        deadline=time.monotonic()+timeout
        previous=None;stable=0
        while time.monotonic()<deadline:
            current=tuple(self.position())
            if math.dist(current,goal)>reach:
                return False
            stable=stable+1 if previous is not None and math.dist(current,previous)<=5.0 else 0
            if stable>=2:
                return True
            previous=current
            time.sleep(0.1)
        return False

    def go_flight(self, goal, *, reach=110.0, timeout=60.0, interrupt=None):
        """Controle 3D por pulsos normais, sem pedidos de rota que desfazem voo.
        Avanca e ganha/perde altura ao mesmo tempo; nao presume trajeto livre.
        """
        state = self._flight_state()
        identity = (state['session'], tuple(state['player_identity']))
        if state['movement']['MovementMode'] != 5:
            stamina = self.reader.flight_stamina_ratio()
            if stamina is None or not math.isfinite(stamina) or stamina < getattr(self, 'min_stamina', FLIGHT_MIN_STAMINA):
                return 'aguardando estamina'
            if state['movement']['MovementMode'] != 1:
                raise RuntimeError('Decolagem exige personagem no solo.')
            self.log('Decolagem normal; o pedido do mapa nao sera reenviado.')
            self.transport.flight_takeoff(self.transport.snapshot())
        self.wait_flight_ready()
        climb=self.fly_vertical(goal, interrupt=interrupt)
        if climb is not True:
            return climb
        if getattr(self, 'mouse_final', True):
            return self.fly_final_with_mouse(goal, reach=reach, interrupt=interrupt)
        self.log('Voo diagonal por pulsos: avancar e ajustar altura em direcao ao recurso. Fluxo continuo desativado.')
        started=time.monotonic()
        best=float('inf');progress_at=started
        speed=500.0;previous=None
        while time.monotonic()-started<timeout:
            if interrupt is not None:
                reason=interrupt()
                if reason:
                    return reason
            state=self._flight_state()
            if (state['session'],tuple(state['player_identity']))!=identity:
                raise RuntimeError('Personagem/sessao mudou durante controle aereo.')
            mode=state['movement']['MovementMode']
            current=tuple(state['position']['position'])
            delta=tuple(b-a for a,b in zip(current,goal))
            distance=math.hypot(*delta);now=time.monotonic()
            if previous is not None:
                dt=max(0.001,now-previous[1])
                step=math.dist(current,previous[0])
                if step>max(600.0,speed*dt*4.0) and distance>previous[2]+600.0:
                    self.log('Retorno grande de posicao observado; voo encerrado sem nova tentativa.')
                    return 'voo interrompido'
                measured=step/dt
                if measured>1:
                    speed=max(100.0,measured)
            entry={'t':round(now,3),'pilot':'input_3d','controller':'pulses',
                   'continuous':False,'dist':round(distance),
                   'horizontal':round(math.hypot(*delta[:2])),
                   'height_remaining':round(delta[2]),'position':current,'movement_mode':mode}
            self.trace.append(entry)
            if distance<=reach:
                return True
            if mode!=5:
                self.log('Voo interrompido: modo aereo perdido; nenhuma nova decolagem.')
                return 'voo interrompido'
            if distance<best-20:
                best,progress_at=distance,now
            elif now-progress_at>3.0:
                self.log('Sem progresso no voo por pulsos; encerrando sem repetir decolagem.')
                return 'voo sem progresso'
            # Simultaneous XYZ approach, rather than an artificial vertical climb
            # followed by horizontal travel. This changes trajectory, not animation fields.
            direction=tuple(v/distance for v in delta)
            remaining=distance-reach
            duration=max(16,min(120,int(500.0*max(10.0,remaining)/speed)))
            if remaining<300.0:
                gain=max(0.2,remaining/300.0)
                direction=tuple(v*gain for v in direction)
                duration=min(duration,80)
            entry.update(direction=direction,duration_ms=duration)
            previous=(current,now,distance)
            self.transport.flight_movement_pulse(self.transport.snapshot(),direction,
                                                 duration_ms=duration,execute=True)
            time.sleep(0.02)
        return 'voo sem progresso'

    # --- ajuste final no alto: usa o cursor por alguns segundos -----------------------------
    def _cursor_guard(self):
        """Coloca o cursor num ponto do jogo que NAO esteja coberto por outra janela (ex.: o console).
        Devolve (hold, restore, ok); ok=False se nenhum ponto livre foi achado."""
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.WindowFromPoint.restype = ctypes.c_void_p; u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.GetAncestor.restype = ctypes.c_void_p; u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        game = self.reader.window
        u.GetForegroundWindow.restype = ctypes.c_void_p; u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        old_fg = u.GetForegroundWindow()
        old = wintypes.POINT(); u.GetCursorPos(ctypes.byref(old))
        rc = wintypes.RECT(); u.GetClientRect(game, ctypes.byref(rc))
        spot = None
        for fy in (0.5, 0.35, 0.65, 0.2, 0.8):
            for fx in (0.5, 0.3, 0.7, 0.15, 0.85):
                pt = wintypes.POINT(int(rc.right * fx), int(rc.bottom * fy)); u.ClientToScreen(game, ctypes.byref(pt))
                hit = u.WindowFromPoint(pt)
                if hit and u.GetAncestor(hit, 2) == game:
                    spot = pt; break
            if spot: break
        class MI(ctypes.Structure):
            _fields_ = [('dx', ctypes.c_long), ('dy', ctypes.c_long), ('mouseData', ctypes.c_ulong),
                        ('dwFlags', ctypes.c_ulong), ('time', ctypes.c_ulong), ('extra', ctypes.c_size_t)]
        class INPUT(ctypes.Structure):
            _fields_ = [('type', ctypes.c_ulong), ('mi', MI)]   # 40 bytes em 64 bits (tamanho exigido)
        def jiggle():
            # micro-movimento pelo mesmo caminho do mouse fisico, para o jogo marcar "mouse em cima"
            for d in (1, -1):
                inp = INPUT(0, MI(d, 0, 0, 0x0001, 0, 0))
                u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        def hold():
            # o jogo so marca "mouse em cima" quando esta em primeiro plano (DiagnosticoMouse, metodo 5)
            if u.GetForegroundWindow() != game: u.SetForegroundWindow(game)
            if spot:
                u.SetCursorPos(spot.x, spot.y); jiggle()
        def restore():
            u.SetCursorPos(old.x, old.y)
            if old_fg and old_fg != game: u.SetForegroundWindow(old_fg)
        hold()
        return hold, restore, spot is not None

    def _mouse_flag(self):
        """Leitura da marcacao do jogo 'mouse sobre a janela' (obj global +0x240)."""
        with self.reader.process(1048576) as p:
            from aion.game_image import game_offset
            obj = struct.unpack('<Q', p.read(p.base + game_offset('mouse_gate_global_rva', 0xEFCC008), 8))[0]
            return p.read(obj + 0x240, 1)[0] if obj else 0

    def _camera(self):
        if getattr(self, 'camera', None) is None:
            from aion.player_nav import PlayerNavigator
            from probe_axis_pulse import send
            self.camera = PlayerNavigator(self.transport, self.reader, self.reader.window, send_axis=send, log=self.log)
            self.camera.per_send = 1.04          # graus por envio medidos no jogo (13/11)
            self.camera.pitch_per_send = -0.67
        return self.camera

    def _w_burst(self, cam, seconds, hold):
        """Segura W (Move_Forward normal) por ~seconds e solta. So funciona com o jogo em primeiro plano."""
        if getattr(self, '_fwd_bind', None) is None:
            self._fwd_bind = self.reader.bindings('Move_Forward')
        base = self.reader.base; end = time.monotonic() + seconds
        try:
            while time.monotonic() < end:
                hold()
                cam.send_axis(self.reader, self.reader.window, base, cam.axis_dll, self._fwd_bind, 'Move_Forward', 1.0, True)
        finally:
            cam.send_axis(self.reader, self.reader.window, base, cam.axis_dll, self._fwd_bind, 'Move_Forward', 0.0, True)
            cam.send_axis(self.reader, self.reader.window, base, cam.axis_dll, self._fwd_bind, 'Move_Forward', 0.0, True, stop=True)

    def fly_final_with_mouse(self, goal, *, reach=110.0, timeout=40.0, interrupt=None):
        """Ajuste final no alto, em passos: para, mira a camera (nivelada) para o recurso, e avanca
        com corrida automatica (longe) ou toques de W dosados (perto). Altura ja ajustada pela subida.
        A corrida nao desliga antes de ~1 s (~1200 de distancia), por isso perto usa W."""
        here = tuple(self.position())
        if math.dist(here, goal) <= reach:
            return True
        self.log(f'Ajuste final no alto ({round(math.dist(here, goal))}): jogo em primeiro plano e mouse por alguns segundos.')
        cam = self._camera()
        hold, restore, free = self._cursor_guard()
        CRUISE = 1150.0      # velocidade da corrida em voo (medida)
        try:
            if not free:
                self.log('Jogo totalmente coberto por outras janelas; nao da para usar o mouse. Pulando spot.')
                return 'voo sem progresso'
            for _ in range(20):
                time.sleep(0.1); hold()
                try:
                    if self._mouse_flag() == 1: break
                except Exception:
                    break
            started = time.monotonic(); w_speed = None
            for step in range(12):
                if time.monotonic() - started > timeout: break
                if interrupt is not None:
                    reason = interrupt()
                    if reason: return reason
                state = self._flight_state()
                if state['movement']['MovementMode'] != 5:
                    self.log('Voo perdido no ajuste final.'); return 'voo interrompido'
                pos = tuple(state['position']['position'])
                horiz = math.dist(pos[:2], goal[:2]); dz = goal[2] - pos[2]
                self.trace.append({'t': round(time.monotonic(), 3), 'pilot': 'final_step', 'step': step,
                                   'horizontal': round(horiz), 'height_remaining': round(dz)})
                self.log(f'Ajuste final: faltam {round(horiz)} na horizontal e {round(dz)} de altura.')
                if horiz <= 150 and abs(dz) <= 200:
                    return True
                if abs(dz) > 250:                         # corrige altura com toque curto
                    button = 'Move_FlyUp' if dz > 0 else 'Move_FlyDown'
                    self.transport.button_action(self.transport.snapshot(), button)
                    time.sleep(max(0.08, min(1.5, abs(dz) / 1280.0 - 0.3)))
                    self._release(button); time.sleep(0.35)
                    continue
                bearing = math.degrees(math.atan2(goal[1] - pos[1], goal[0] - pos[0]))
                hold(); cam.face(bearing, tolerance=3.0); hold(); cam.set_pitch(0.0, tolerance=4.0, rounds=3)
                before = pos
                if horiz > 1500:
                    run = max(1.05, (horiz - 1200.0) / CRUISE)
                    hold(); self.transport.autorun_toggle(self.transport.snapshot()); self._running_final = True
                    t0 = time.monotonic()
                    while time.monotonic() - t0 < run:
                        time.sleep(0.1); hold()
                        if interrupt is not None:
                            reason = interrupt()
                            if reason: break
                    hold(); self.transport.autorun_toggle(self.transport.snapshot()); self._running_final = False
                else:
                    v = w_speed or 900.0
                    secs = max(0.12, min(1.2, (horiz - 80.0) / v))
                    t0 = time.monotonic(); self._w_burst(cam, secs, hold); dt = time.monotonic() - t0
                time.sleep(0.6)                           # deixa o embalo terminar antes de medir
                after = tuple(self._flight_state()['position']['position'])
                moved = math.dist(before[:2], after[:2])
                if horiz <= 1500 and dt > 0.05 and moved > 20:
                    w_speed = max(200.0, moved / dt)      # aprende distancia por segundo de W (inclui embalo)
                if moved < 20:
                    self.log('Ajuste final: nao saiu do lugar (obstaculo ou W bloqueado).')
                    if step >= 2: return 'voo sem progresso'
            self.log('Ajuste final: nao chegou em 12 passos.')
            return 'voo sem progresso'
        finally:
            if getattr(self, '_running_final', False):
                try: hold(); self.transport.autorun_toggle(self.transport.snapshot()); self._running_final = False
                except Exception as exc: self.log(f'Falha ao desligar corrida: {exc}')
            restore()

    def approach_below(self, goal, *, horizontal_reach=250.0, timeout=60.0, interrupt=None):
        """Pelo chao (rota do jogo, funciona com mouse fora) ate ficar embaixo/perto do recurso alto.
        Para quando a distancia horizontal fica pequena ou quando trava no pe do morro."""
        here = self.position()
        if math.dist(here[:2], goal[:2]) <= horizontal_reach:
            return True
        self.log(f'Pelo chao ate embaixo do recurso ({round(math.dist(here[:2], goal[:2]))} na horizontal).')
        if not self.mount():
            return 'sem montaria'
        if not self.send_goal(goal):
            return 'aguardando estamina'
        best = math.dist(here[:2], goal[:2]); best_t = time.monotonic(); deadline = best_t + timeout
        try:
            while time.monotonic() < deadline:
                time.sleep(0.25)
                if interrupt is not None:
                    reason = interrupt()
                    if reason:
                        return reason
                cur = self.position(); horiz = math.dist(cur[:2], goal[:2])
                self.trace.append({'t': round(time.monotonic(), 2), 'pilot': 'ground_below', 'horizontal': round(horiz),
                                   'height_remaining': round(goal[2] - cur[2])})
                if horiz <= horizontal_reach:
                    return True
                if horiz < best - 50:
                    best, best_t = horiz, time.monotonic()
                elif time.monotonic() - best_t > 3.0 or horiz > best + 400:
                    self.log(f'Parou no pe do morro a {round(horiz)} na horizontal; subindo daqui.')
                    return True
            return True
        finally:
            self.stop()
            time.sleep(0.6)

    def fly_vertical(self, goal, *, tolerance=150.0, lead=0.35, timeout=25.0, interrupt=None):
        """Sobe/desce segurando Move_FlyUp/Move_FlyDown do jogo (funciona com o mouse fora).
        Solta antes da altura alvo (lead s * velocidade medida) para compensar o embalo."""
        state = self._flight_state()
        identity = (state['session'], tuple(state['player_identity']))
        dz = goal[2] - state['position']['position'][2]
        if abs(dz) <= tolerance:
            return True
        button = 'Move_FlyUp' if dz > 0 else 'Move_FlyDown'
        self.log(f"{'Subindo' if dz > 0 else 'Descendo'} {abs(round(dz))} de altura com {button}.")
        speed = 1280.0
        started = time.monotonic(); last = (state['position']['position'][2], started)
        self.transport.button_action(self.transport.snapshot(), button)
        try:
            while time.monotonic() - started < timeout:
                time.sleep(0.05)
                if interrupt is not None:
                    reason = interrupt()
                    if reason:
                        return reason
                state = self._flight_state()
                if (state['session'], tuple(state['player_identity'])) != identity:
                    raise RuntimeError('Personagem/sessao mudou durante subida.')
                if state['movement']['MovementMode'] != 5:
                    self.log('Voo perdido durante subida/descida.')
                    return 'voo interrompido'
                z = state['position']['position'][2]; now = time.monotonic()
                if now - last[1] > 0.04:
                    measured = abs(z - last[0]) / (now - last[1])
                    if measured > 50: speed = 0.5 * speed + 0.5 * measured
                last = (z, now)
                remaining = goal[2] - z
                self.trace.append({'t': round(now, 3), 'pilot': 'fly_vertical', 'button': button,
                                   'height_remaining': round(remaining), 'speed': round(speed)})
                if (remaining if dz > 0 else -remaining) <= max(tolerance, speed * lead):
                    return True
            self.log('Subida/descida sem chegar a altura no tempo limite.')
            return 'voo sem progresso'
        finally:
            self._release(button)

    def go(self, goal, *, reach=110.0, timeout=120.0, max_retries=3, interrupt=None):
        goal = tuple(goal)
        self.expect_flight = False
        if interrupt is not None:
            reason = interrupt()
            if reason:
                self.stop()
                return reason
        if math.dist(self.position(), goal) <= reach:
            self.stop()
            return True
        if getattr(self, 'flight', False):
            mode = self._flight_state()['movement']['MovementMode']
            if mode == 5 or abs(goal[2] - self.position()[2]) > 300.0:
                if mode != 5:
                    below = self.approach_below(goal, interrupt=interrupt)
                    if below is not True:
                        return below
                return self.go_flight(goal,reach=reach,timeout=min(timeout,60.0),interrupt=interrupt)
        if not self.mount():
            return 'sem montaria'
        if not self.send_goal(goal):
            return 'aguardando estamina'
        retries = 0; deadline = time.monotonic() + timeout
        best = math.dist(self.position(), goal); best_t = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.25)
            if interrupt is not None:
                reason = interrupt()
                if reason:
                    self.stop(); self.log(f'Parando: {reason}'); return reason
            current = self.position()
            dist = math.dist(current, goal)
            self.trace.append({'t': round(time.monotonic(), 2), 'dist': round(dist),
                               'horizontal': round(math.dist(current[:2], goal[:2])),
                               'height_remaining': round(goal[2] - current[2])})
            if getattr(self, 'flight', False):
                self.trace[-1]['flight'] = self.flight_telemetry()
                if getattr(self, 'expect_flight', False) and dist > reach and self.trace[-1]['flight']['movement_modes'][0] != 5:
                    self.stop()
                    self.log('Modo de voo perdido apos envio do destino; teste encerrado para analise.')
                    return 'voo interrompido'
            if dist <= reach:
                self.stop()
                return True
            if dist < best - 50:
                best, best_t = dist, time.monotonic()
            elif time.monotonic() - best_t > 4.0:
                if getattr(self, 'flight', False):
                    self.stop()
                    self.log('Voo sem progresso: encerrando para analisar altura e modo, sem repetir decolagem.')
                    return 'voo sem progresso'
                retries += 1
                if retries > max_retries:
                    self.stop(); self.log('Não consegui chegar (sem progresso).'); return False
                self.log('Sem progresso: cancelando e pedindo o destino de novo.')
                self.stop()
                if not self.mount():
                    self.stop(); return 'sem montaria'
                if not self.send_goal(goal):
                    return 'aguardando estamina'
                best_t = time.monotonic()
        self.stop(); self.log('Tempo esgotado.'); return False
