"""Segue as missões marcadas na ordem do painel, clicando no rastreador do jogo."""
import math
import time

ARRIVE_STILL_S = 4.0     # parado por esse tempo = chegou
TRAVEL_TIMEOUT_S = 240.0
IDLE_S = 12.0            # sem inimigo por esse tempo = area limpa
SMALL_MOVE = 800.0       # deslocamento menor indica que o personagem já estava perto
STUCK_LIMIT = 2
CLICK_RETRY_S = 2.0
TRACKER_INTERVAL_S = 1.5
AT_OBJECTIVE = object()


def _send_quest_shortcut(hwnd, slot):
    """Use the tracker hotkey when the player has already focused the game."""
    from ctypes import byref, sizeof
    from .auto_relog import INPUT, KI, user32

    def send(scan, flags):
        event = INPUT(type=1)
        event.ki = KI(0, scan, flags, 0, 0)
        return user32.SendInput(1, byref(event), sizeof(INPUT)) == 1

    pressed = []
    try:
        for scan in (0x38, 0x01 + slot):
            if user32.GetForegroundWindow() != hwnd or not send(scan, 0x0008):
                break
            pressed.append(scan)
            time.sleep(0.12)
    finally:
        released = [send(scan, 0x000A) for scan in reversed(pressed)]
    return len(pressed) == 2 and all(released)


def _post_quest_shortcut(post, hwnd, slot):
    """Send Alt+digit as window messages without changing keyboard focus."""
    alt, digit = 0x12, ord(str(slot))
    alt_scan, digit_scan = 0x38 << 16, (slot + 1) << 16
    alt_context, key_up = 1 << 29, (1 << 30) | (1 << 31)
    if not post(hwnd, 0x0104, alt, 1 | alt_scan):  # WM_SYSKEYDOWN
        return False
    try:
        time.sleep(0.08)
        if not post(hwnd, 0x0104, digit, 1 | digit_scan | alt_context):
            return False
        try:
            time.sleep(0.12)
        finally:
            digit_released = post(hwnd, 0x0105, digit, 1 | digit_scan | alt_context | key_up)
    finally:
        alt_released = post(hwnd, 0x0105, alt, 1 | alt_scan | key_up)  # WM_SYSKEYUP
    return bool(digit_released and alt_released)


def click_quest(hwnd, slot, expected_title=None):
    from ctypes import byref, windll
    from ctypes.wintypes import HWND, LPARAM, POINT, UINT, WPARAM
    from .quest_tracker import capture_quest_tracker, find_quest_row, quest_row_selected
    from .quest_tracker import quest_title_key, quest_title_preview, same_quest_title
    from .quest_tracker import quest_at_objective
    try:
        from PIL import ImageGrab
    except ImportError:
        return 'Pillow é necessário para reconhecer o rastreador.'

    try:
        from PIL import ImageGrab
    except ImportError:
        return 'Pillow é necessário para reconhecer o rastreador.'
    if not hwnd:
        return 'a janela do jogo está indisponível.'
    user32 = windll.user32
    try:
        screenshot, bbox = capture_quest_tracker(hwnd)
    except (OSError, RuntimeError):
        return 'não foi possível capturar o rastreador do jogo.'
    match = find_quest_row(screenshot, slot)
    if match is None:
        return 'mantenha o rastreador de missões visível para localizar esta missão.'
    title, scale = match
    if expected_title and not same_quest_title(
            expected_title, quest_title_key(quest_title_preview(screenshot, title, scale), scale)):
        return 'a missão nesta posição mudou. Atualize a lista e selecione novamente.'
    at_objective = bool(expected_title and quest_at_objective(screenshot, title, scale))
    if at_objective and quest_row_selected(screenshot, title, scale):
        return AT_OBJECTIVE

    def selection_confirmed():
        for _ in range(3):
            time.sleep(0.16)
            try:
                frame, _ = capture_quest_tracker(hwnd)
                if quest_row_selected(frame, title, scale):
                    return True
                # Some game frames are presented on the desktop before window capture updates.
                if user32.GetForegroundWindow() == hwnd:
                    frame = ImageGrab.grab(bbox=bbox, all_screens=True)
                    if quest_row_selected(frame, title, scale):
                        return True
            except (OSError, RuntimeError):
                break
        return False

    post = user32.PostMessageW
    post.argtypes = [HWND, UINT, WPARAM, LPARAM]
    if user32.GetForegroundWindow() == hwnd:
        if _send_quest_shortcut(hwnd, slot) and selection_confirmed():
            return AT_OBJECTIVE if at_objective else True

    shortcut_posted = _post_quest_shortcut(post, hwnd, slot)
    if shortcut_posted and selection_confirmed():
        return AT_OBJECTIVE if at_objective else True

    screen = POINT(bbox[0] + title[0], bbox[1] + title[1])
    user32.WindowFromPoint.argtypes = [POINT]
    user32.WindowFromPoint.restype = HWND
    user32.GetAncestor.argtypes = [HWND, UINT]
    user32.GetAncestor.restype = HWND
    hit = user32.WindowFromPoint(screen)
    recipient = hit if hit and user32.GetAncestor(hit, 2) == hwnd else hwnd
    client = POINT(screen.x, screen.y)
    if not user32.ScreenToClient(recipient, byref(client)):
        return 'não foi possível localizar o clique na janela do jogo.'
    coords = (client.x & 0xffff) | ((client.y & 0xffff) << 16)
    if not post(recipient, 0x0200, 0, coords):  # WM_MOUSEMOVE
        return 'o Windows recusou a posição do clique na janela do jogo.'
    time.sleep(0.1)
    if not post(recipient, 0x0201, 1, coords):  # WM_LBUTTONDOWN
        return 'o Windows recusou o clique direto na janela do jogo.'
    try:
        time.sleep(0.12)
    finally:
        released = post(recipient, 0x0202, 0, coords)  # WM_LBUTTONUP
    if not released:
        return 'o Windows recusou a liberação do clique na janela do jogo.'
    if selection_confirmed():
        return AT_OBJECTIVE if at_objective else True
    if at_objective:
        return 'já no objetivo. Selecione a missão uma vez no rastreador do jogo para liberar o combate.'
    shortcut = 'sem confirmação' if shortcut_posted else 'recusado pelo Windows'
    return f'Alt+{slot} enviado à janela: {shortcut}. Clique direto sem confirmação.'


class QuestDriver:
    def __init__(self, slots, press=None, clock=time.monotonic, *, titles=None, read_tracker=None):
        self.slots = [int(s) for s in slots if 1 <= int(s) <= 5] or [1]
        self.press = press
        self.clock = clock
        self.index = 0
        self.phase = 'start'
        self.stuck = 0
        self.retry_at = 0.0
        self.retry_message = ''
        self.last_click = None
        self.titles = titles or {}
        self.read_tracker = read_tracker
        self.tracker_at = 0.0
        self.current_visible_slot = None
        self.current_seen = False
        self.missing_reads = 0

    @property
    def slot(self):
        return self.slots[self.index % len(self.slots)]

    def _go(self, position, now):
        self.phase = 'start'
        if now < self.retry_at:
            return 'waiting', self.retry_message
        result = self.press(self.current_visible_slot, self.titles[str(self.slot)]) if self.read_tracker else self.press(self.slot)
        self.last_click = {'slot': self.slot, 'confirmed': result is True or result is AT_OBJECTIVE,
                           'message': result if isinstance(result, str) else ''}
        if result is AT_OBJECTIVE:
            self.phase = 'fight'
            self.last_engaged = now
            self.last_click['at_objective'] = True
            return 'fight', f'Missão {self.slot}: já está no objetivo. Procurando alvos marcados.'
        if isinstance(result, str) or result is False:
            reason = result or 'aguardando um clique válido.'
            self.retry_at = now + CLICK_RETRY_S
            self.retry_message = f'Missão {self.slot}: {reason}'
            return 'waiting', self.retry_message
        self.phase = 'travel'
        self.travel_start = now
        self.travel_origin = position
        self.last_pos = position
        self.last_move = now
        return 'travel', f'Missão {self.slot}: indo até o objetivo.'

    def _next_quest(self, position, now):
        self.index = (self.index + 1) % len(self.slots)
        self.stuck = 0
        return self._go(position, now)

    def _check_tracker(self, now):
        from .quest_tracker import same_quest_title

        reward_pending = False
        if now >= self.tracker_at:
            self.tracker_at = now + TRACKER_INTERVAL_S
            try:
                visible, rewards = self.read_tracker()
            except (ImportError, OSError, RuntimeError):
                visible, rewards = {}, set()
            expected = self.titles.get(str(self.slot))
            matches = [slot for slot, key in visible.items() if same_quest_title(expected, key)]
            self.current_visible_slot = matches[0] if len(matches) == 1 else None
            if self.current_visible_slot is not None:
                self.current_seen = True
                self.missing_reads = 0
                reward_pending = self.current_visible_slot in rewards
            elif not matches and len(visible) >= 2 and (
                    self.current_seen or any(
                        sum(same_quest_title(self.titles.get(str(slot)), key) for key in visible.values()) == 1
                        for slot in self.slots[self.index + 1:])):
                self.missing_reads += 1
            else:
                self.missing_reads = 0
        if reward_pending or self.missing_reads >= 2:
            previous = self.slot
            self.index += 1
            if self.index == len(self.slots):
                self.phase = 'done'
                return 'done', 'Sequência de missões encerrada. Recolha as recompensas pendentes no jogo.'
            self.phase = 'start'
            self.current_seen = False
            self.missing_reads = 0
            self.retry_at = self.tracker_at = now
            reason = ('concluída, com recompensa pendente' if reward_pending else
                      'não está mais no rastreador')
            return 'waiting', f'Missão {previous} {reason}. Buscando a próxima missão marcada.'
        if self.current_visible_slot is None:
            return 'waiting', f'Missão {self.slot}: aguardando confirmação no rastreador visível.'
        return None

    def tick(self, position, engaged, now=None):
        """position: (x,y,z) ou None; engaged: ha inimigo valido marcado. Devolve (fase, mensagem)."""
        now = self.clock() if now is None else now
        if self.phase == 'done':
            return 'done', 'Sequência de missões encerrada. Recolha as recompensas pendentes no jogo.'
        if self.read_tracker:
            tracker_state = self._check_tracker(now)
            if tracker_state is not None:
                return tracker_state
        if self.phase == 'start':
            return self._go(position, now)
        if self.phase == 'travel':
            if position is not None and self.last_pos is not None and math.dist(position, self.last_pos) > 150:
                self.last_pos = position; self.last_move = now
            arrived = now - self.last_move >= ARRIVE_STILL_S and now - self.travel_start >= 3.0
            if arrived or now - self.travel_start > TRAVEL_TIMEOUT_S:
                moved = (math.dist(position, self.travel_origin)
                         if position is not None and self.travel_origin is not None else SMALL_MOVE + 1)
                self.stuck = self.stuck + 1 if moved < SMALL_MOVE else 0
                self.phase = 'fight'; self.last_engaged = now
                return 'fight', f'Missão {self.slot}: chegou, procurando inimigos.'
            return 'travel', f'Missão {self.slot}: indo até o objetivo…'
        # fight
        if engaged:
            self.last_engaged = now; self.stuck = 0
            return 'fight', f'Missão {self.slot}: atacando.'
        if now - self.last_engaged >= IDLE_S:
            if not self.read_tracker and self.stuck >= STUCK_LIMIT and len(self.slots) > 1:
                return self._next_quest(position, now)
            return self._go(position, now)
        return 'fight', f'Missão {self.slot}: procurando inimigos.'
