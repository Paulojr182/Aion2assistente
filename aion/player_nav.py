"""Navegação como jogador seguindo a rota do próprio jogo.

Usa só ações normais do jogo (como o auto ataque): corrida automática (Move_AutoForward),
giro de câmera (Camera_RotateX), corrida rápida (Move_Sprint) e montaria (Move_Ride).
A rota vem do pathfinding do jogo (MoveToLocation planejado e cancelado na mesma chamada).

Controle de direção por "perseguição de ponto à frente" (pure pursuit): mira num ponto sobre
a linha da rota a uma distância proporcional à velocidade, o que evita passar dos pontos e
cortar curvas. Giro calibrado parado; travamento ou desvio grande pedem nova rota ao jogo.
"""
import math
import statistics
import struct
import time

CAM_YAW_OFFSET = 0x400   # PlayerCameraManager: ângulo horizontal da câmera (double)
CAM_PITCH_OFFSET = 0x3F8  # PlayerCameraManager: ângulo vertical (double); positivo + W em voo = subir


def wrap(deg):
    return (deg + 180.0) % 360.0 - 180.0


def project(pos, a, b):
    """Projeção de pos no segmento a-b: (t em [0,1], ponto, distância lateral)."""
    ax, ay = a[0], a[1]; bx, by = b[0], b[1]
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 < 1e-6 else max(0.0, min(1.0, ((pos[0] - ax) * dx + (pos[1] - ay) * dy) / length2))
    px, py = ax + t * dx, ay + t * dy
    return t, (px, py), math.hypot(pos[0] - px, pos[1] - py)


def lookahead_point(path, pos, distance, start_index=0):
    """Ponto da polilinha a 'distance' à frente da projeção de pos (a partir de start_index)."""
    best = None
    for i in range(start_index, len(path) - 1):
        t, point, lateral = project(pos, path[i], path[i + 1])
        if best is None or lateral < best[2] - 1e-6:
            best = (i, t, lateral)
    if best is None:
        return path[-1], len(path) - 1, 0.0
    i, t, lateral = best
    a, b = path[i], path[i + 1]
    seg = math.dist(a[:2], b[:2])
    remaining = distance + t * seg
    j = i
    while j < len(path) - 1:
        seg = math.dist(path[j][:2], path[j + 1][:2])
        if remaining <= seg or j == len(path) - 2:
            f = 1.0 if seg < 1e-6 else min(1.0, remaining / seg)
            p, q = path[j], path[j + 1]
            return (p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f, p[2] + (q[2] - p[2]) * f), i, lateral
        remaining -= seg
        j += 1
    return path[-1], i, lateral


class PlayerNavigator:
    def __init__(self, transport, reader, window, *, send_axis, log=print, ride=False, sprint=False):
        from aion.native_player import ObjectRegistry
        from aion.reflection_fields import ReflectionSchema
        self.transport, self.reader, self.window = transport, reader, window
        self.send_axis = send_axis          # send(reader, window, base, artifact, bindings, axis, value, execute, stop=False)
        self.log, self.ride, self.sprint = log, ride, sprint
        self.rot = reader.bindings('Camera_RotateX')
        self.axis_dll = reader.load('native/probe_build.json')['artifacts']['axis_pulse']
        with reader.process(4194304) as process:
            registry = ObjectRegistry(process, reader.roots)
            schema = ReflectionSchema(registry, max_bytes=2097152)
            controller = registry.object(self.rot['frame_objects']['controller']['address'])
            self.cam = schema.linked(controller, 'PlayerController', 'PlayerCameraManager')['address']
        self.per_send = None
        self.pitch_bind = None; self.pitch_per_send = None
        self.running = False; self.last_toggle = 0.0; self.sprinting = False
        self.trace = []

    # --- leituras -------------------------------------------------------------------------
    def position(self):
        from aion.native_flight import observe_flight_state
        with self.reader.process(2097152) as process:
            return observe_flight_state(process, self.reader.roots, with_position=True)['position']['position']

    def yaw(self):
        with self.reader.process(1048576) as process:
            return struct.unpack('<d', process.read(self.cam + CAM_YAW_OFFSET, 8))[0]

    def pitch(self):
        with self.reader.process(1048576) as process:
            return struct.unpack('<d', process.read(self.cam + CAM_PITCH_OFFSET, 8))[0]

    # --- ações ----------------------------------------------------------------------------
    def rotate(self, sends, value):
        base = self.reader.base
        for _ in range(sends):
            self.send_axis(self.reader, self.window, base, self.axis_dll, self.rot, 'Camera_RotateX', value, True)
            time.sleep(0.01)
        self.send_axis(self.reader, self.window, base, self.axis_dll, self.rot, 'Camera_RotateX', 0.0, True)
        self.send_axis(self.reader, self.window, base, self.axis_dll, self.rot, 'Camera_RotateX', 0.0, True, stop=True)

    def rotate_pitch(self, sends, value):
        if self.pitch_bind is None: self.pitch_bind = self.reader.bindings('Camera_RotateY')
        base = self.reader.base
        for _ in range(sends):
            self.send_axis(self.reader, self.window, base, self.axis_dll, self.pitch_bind, 'Camera_RotateY', value, True)
            time.sleep(0.01)
        self.send_axis(self.reader, self.window, base, self.axis_dll, self.pitch_bind, 'Camera_RotateY', 0.0, True)
        self.send_axis(self.reader, self.window, base, self.axis_dll, self.pitch_bind, 'Camera_RotateY', 0.0, True, stop=True)

    def calibrate_pitch(self):
        samples = []
        for value in (1.0, -1.0, 1.0):
            before = self.pitch(); self.rotate_pitch(6, value); time.sleep(0.12)
            moved = self.pitch() - before
            if abs(moved) > 0.3: samples.append(moved / (6 * value))
        if not samples:
            raise RuntimeError('O pitch da câmera não respondeu na calibração.')
        sign = math.copysign(1.0, statistics.median(samples))
        self.pitch_per_send = sign * max(0.2, min(8.0, statistics.median(abs(x) for x in samples)))
        self.log(f'Calibração do pitch: {self.pitch_per_send:.2f} graus por envio')

    def set_pitch(self, target, tolerance=2.0, rounds=6):
        """Inclina a câmera até target graus (positivo = para cima)."""
        if self.pitch_per_send is None: self.calibrate_pitch()
        err = target - self.pitch()
        for _ in range(rounds):
            if abs(err) <= tolerance: break
            sends = max(1, min(60, round(abs(err) / abs(self.pitch_per_send))))
            self.rotate_pitch(sends, 1.0 if err * self.pitch_per_send > 0 else -1.0)
            time.sleep(0.06)
            err = target - self.pitch()
        return err

    def set_running(self, on):
        if self.running == on: return
        wait = 1.05 - (time.monotonic() - self.last_toggle)
        if wait > 0: time.sleep(wait)
        self.transport.autorun_toggle(self.transport.snapshot(), execute=True)
        self.running = on; self.last_toggle = time.monotonic()
        if on and self.sprint and not self.sprinting:
            self.transport.button_action(self.transport.snapshot(), 'Move_Sprint'); self.sprinting = True
        if not on and self.sprinting:
            self.transport.button_action(self.transport.snapshot(), 'Move_Sprint', release=True); self.sprinting = False

    def calibrate(self):
        """Mede graus por envio de +1 com a personagem parada (mediana de 3 medições)."""
        samples = []
        for value in (1.0, -1.0, 1.0):
            before = self.yaw(); self.rotate(8, value); time.sleep(0.12)
            moved = wrap(self.yaw() - before)
            if abs(moved) > 0.5: samples.append(moved / (8 * value))
        if not samples:
            raise RuntimeError('O giro da câmera não respondeu na calibração.')
        sign = math.copysign(1.0, statistics.median(samples))
        magnitude = statistics.median(abs(s) for s in samples)
        self.per_send = sign * max(0.3, min(8.0, magnitude))
        self.log(f'Calibração do giro: {self.per_send:.2f} graus por envio')

    def face(self, bearing, tolerance=3.0, rounds=6):
        """Gira parado até apontar para bearing (realimentado pelo ângulo real)."""
        err = wrap(bearing - self.yaw())
        for _ in range(rounds):
            if abs(err) <= tolerance: break
            sends = max(1, min(80, round(abs(err) / abs(self.per_send))))
            self.rotate(sends, 1.0 if err * self.per_send > 0 else -1.0)
            time.sleep(0.06)
            err = wrap(bearing - self.yaw())
        return err

    def steer(self, err):
        """Correção pequena em movimento (não reaprende o ganho)."""
        sends = max(1, min(10, round(0.7 * abs(err) / abs(self.per_send))))
        self.rotate(sends, 1.0 if err * self.per_send > 0 else -1.0)

    # --- rota -----------------------------------------------------------------------------
    def plan(self, goal):
        pos = self.position()
        route = [tuple(p) for p in self.transport.plan_route(self.transport.snapshot(), goal)]
        path = [tuple(pos)] + [p for p in route if math.dist(p[:2], pos[:2]) > 60]
        if math.dist(path[-1][:2], goal[:2]) > 60:
            path.append(tuple(goal))
        self.log(f'Rota do jogo: {len(route)} pontos')
        return path

    # --- laço principal -------------------------------------------------------------------
    def go(self, goal, *, reach=110.0, timeout=120.0, max_replans=3, interrupt=None):
        """Vai até goal. Retorna True (chegou), False (falhou) ou o valor de interrupt() quando ele pede parada."""
        goal = tuple(goal)
        if self.per_send is None: self.calibrate()
        if self.ride:
            r = self.transport.button_action(self.transport.snapshot(), 'Move_Ride')
            self.log(f"Montaria enviada: {r.get('autorun_dispatched')}"); time.sleep(2.5)
        path = self.plan(goal); seg = 0; replans = 0
        deadline = time.monotonic() + timeout
        last_pos, last_t = self.position(), time.monotonic(); speed = 0.0
        progress_ref, progress_t = math.dist(last_pos[:2], goal[:2]), time.monotonic()
        try:
            while time.monotonic() < deadline:
                pos = self.position(); now = time.monotonic()
                dt = max(1e-3, now - last_t)
                speed = 0.6 * speed + 0.4 * (math.dist(pos[:2], last_pos[:2]) / dt)
                last_pos, last_t = pos, now
                dist_goal = math.dist(pos[:2], goal[:2])
                if interrupt is not None:
                    reason = interrupt()
                    if reason:
                        self.log(f'Parando: {reason}'); return reason
                if dist_goal <= reach:
                    self.log(f'Chegou ao destino ({dist_goal:.0f}).'); return True
                lookahead = max(250.0, min(900.0, 0.55 * speed + 150.0))
                if dist_goal < lookahead: carrot, seg_i, lateral = goal, len(path) - 1, 0.0
                else: carrot, seg_i, lateral = lookahead_point(path, pos, lookahead, seg)
                seg = max(seg, min(seg_i, len(path) - 2))
                # travamento ou desvio grande -> nova rota do jogo a partir daqui
                if dist_goal < progress_ref - 80: progress_ref, progress_t = dist_goal, now
                stuck = self.running and now - progress_t > 2.5
                if stuck or lateral > 600:
                    replans += 1
                    self.log('Travado/fora da rota: pedindo nova rota' if stuck else 'Fora da rota: pedindo nova rota')
                    if replans > max_replans:
                        self.log('Desisti: muitas tentativas de rota.'); return False
                    self.set_running(False); time.sleep(0.3)
                    path = self.plan(goal); seg = 0
                    progress_ref, progress_t = math.dist(self.position()[:2], goal[:2]), time.monotonic()
                    continue
                bearing = math.degrees(math.atan2(carrot[1] - pos[1], carrot[0] - pos[0]))
                err = wrap(bearing - self.yaw())
                if not self.running or abs(err) > 60:
                    self.set_running(False)
                    err = self.face(bearing)
                    self.set_running(True)
                    progress_t = time.monotonic()
                elif abs(err) > 4:
                    self.steer(err)
                self.trace.append({'pos': [round(v) for v in pos], 'goal': round(dist_goal), 'speed': round(speed),
                                   'look': round(lookahead), 'lat': round(lateral), 'err': round(err, 1), 'seg': seg})
                time.sleep(0.08)
            self.log('Tempo esgotado.'); return False
        finally:
            try: self.set_running(False)
            except Exception as exc: self.log(f'Falha ao parar: {exc}')
