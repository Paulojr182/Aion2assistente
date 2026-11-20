"""Locate quest rows from the visible Alt key labels and colored titles."""
import re


def capture_quest_tracker(hwnd, *, full_window=False):
    from ctypes import byref, windll
    from ctypes.wintypes import HWND, POINT, RECT, UINT
    from PIL import ImageGrab

    user32 = windll.user32
    bounds, window_rect, origin = RECT(), RECT(), POINT()
    if not (hwnd and user32.GetClientRect(hwnd, byref(bounds))
            and user32.GetWindowRect(hwnd, byref(window_rect))
            and user32.ClientToScreen(hwnd, byref(origin))
            and bounds.right > 0 and bounds.bottom > 0):
        raise RuntimeError('Não foi possível localizar a janela do jogo.')
    left = origin.x if full_window else origin.x + bounds.right // 2
    bbox = (left, origin.y, origin.x + bounds.right, origin.y + bounds.bottom)
    user32.WindowFromPoint.argtypes = [POINT]
    user32.WindowFromPoint.restype = HWND
    user32.GetAncestor.argtypes = [HWND, UINT]
    user32.GetAncestor.restype = HWND
    right = bbox[2]
    columns = (right - min(35, bounds.right // 16), right - min(220, bounds.right // 4))
    if full_window:
        columns += (origin.x + bounds.right // 4, origin.x + bounds.right // 2)
    samples = (POINT(x, origin.y + round(bounds.bottom * fraction))
               for x in columns for fraction in (0.26, 0.37, 0.49, 0.62, 0.75, 0.85))
    if all((hit := user32.WindowFromPoint(point)) and user32.GetAncestor(hit, 2) == hwnd
           for point in samples):
        try:
            return ImageGrab.grab(bbox=bbox, all_screens=True), bbox
        except OSError:
            pass
    try:
        image = ImageGrab.grab(window=hwnd)
    except TypeError as exc:
        raise RuntimeError('A versão do Pillow não oferece captura de janela em segundo plano.') from exc
    crop = (left - window_rect.left, origin.y - window_rect.top,
            bbox[2] - window_rect.left, bbox[3] - window_rect.top)
    return image.crop(crop), bbox


_LABEL = (
    '#...#.....',
    '##..#.##..',
    '.#..#.##..',
    '###.#.##..',
    '#.#.#.##..',
)
_DIGITS = {
    1: ('..##..', '...#..', '...#..', '...#..', '...#..'),
    2: ('.####.', '....#.', '...#..', '..#...', '.####.'),
    3: ('.####.', '....#.', '...#..', '....#.', '.####.'),
    4: ('...#..', '..##..', '.#.#..', '.####.', '...#..'),
    5: ('..#...', '..#...', '..##..', '....#.', '.###..'),
}
_ANCHOR = re.compile(b'\x01\x00\x00\x00\x01')
# White glyph mask for the Portuguese tracker line "Toque para receber a recompensa".
_REWARD_PROMPT = (
    0x000000000000000000000000300000000000000000000000,
    0x7e0000000000000000000000300000000000000000000000,
    0x080000000000000000000000300000000000000000000000,
    0x087c7e467c1f0f3cf07be79f3e3e783c1ef9e7c7fc7c7cf8,
    0x0866664644198130106226193322600418898666666644cc,
    0x084646467c198f30f063e41f333e603c18f9046666667ccc,
    0x08464666401991311062041033306044188104666666408c,
    0x0864666660191331306306183330604c18c186466664608c,
    0x0838363e3c1f1d31d061e38f2e1e60741878e386667c3c8c,
    0x000006000018000000000000000000000000000000600000,
    0x000006000018000000000000000000000000000000600000,
    0x000004000010000000000000000000000000000000400000,
)


def _colors(image):
    return (image.get_flattened_data() if hasattr(image, 'get_flattened_data')
            else image.getdata())


def quest_row_selected(image, point, scale):
    x, y = point
    region = image.convert('RGB').crop(tuple(round(value) for value in
        (x - 52 * scale, y + 5 * scale, x - 28 * scale, y + 31 * scale)))
    blue = sum(b > 105 and g > 78 and b > r + 30 and g > r + 18
               for r, g, b in _colors(region))
    return blue >= region.width * region.height * 0.55


def _difference(pixels, stride, x, y, pattern):
    return sum(
        pixels[(y + row) * stride + x + col] != (symbol == '#')
        for row, line in enumerate(pattern)
        for col, symbol in enumerate(line)
    )


def _find_rows(image):
    from PIL import ImageChops

    red, green, blue = image.split()
    white = ImageChops.darker(ImageChops.darker(red, green), blue)
    pixels = white.point([int(v >= 165) for v in range(256)]).tobytes()
    width, height = image.size
    rows = []
    for match in _ANCHOR.finditer(pixels):
        y, x = divmod(match.start(), width)
        if x + 48 >= width or not 20 <= y < height - 6:
            continue
        if _difference(pixels, width, x, y, _LABEL) > 7:
            continue
        scores = {slot: _difference(pixels, width, x + 17, y + 1, digit)
                  for slot, digit in _DIGITS.items()}
        scores[2] = min(scores[2], _difference(
            pixels, width, x + 17, y + 1, ('..##..', '.####.', '...#..', '..#...', '.####.')))
        scores[3] = min(scores[3], _difference(
            pixels, width, x + 17, y + 1, ('.#..#.', '...##.', '....#.', '.#..#.', '..##..')))
        scores = sorted((score, slot) for slot, score in scores.items())
        if scores[0][0] > 4 or scores[0][0] == scores[1][0]:
            continue
        title_y = y - 20
        title = image.crop((x + 30, title_y - 4, min(x + 135, width), title_y + 5))
        if sum((r > 135 and b > 130 and min(r, b) - g > 34)
               or (r > 160 and g > 95 and g - b > 50)
               for r, g, b in _colors(title)) < 24:
            continue
        rows.append((scores[0][1], x, title_y))
    return rows


def find_quest_rows(image):
    """Return the title point and UI scale for each unique tracker row."""
    from PIL import Image

    image = image.convert('RGB')
    found = {}
    ambiguous = set()
    for scale in (1.0, 1.25, 1.5, 0.8):
        sample = image if scale == 1.0 else image.resize(
            (round(image.width / scale), round(image.height / scale)), Image.Resampling.BILINEAR)
        rows = _find_rows(sample)
        for slot, x, y in rows:
            if slot in found or slot in ambiguous:
                continue
            if sum(number == slot for number, _, _ in rows) > 1:
                ambiguous.add(slot)
                continue
            found[slot] = (round((x + 46) * scale), round(y * scale)), scale
        if len(found) == len(_DIGITS):
            break
    return found


def find_quest_row(image, slot):
    return find_quest_rows(image).get(slot)


def quest_title_preview(image, point, scale):
    x, y = point
    return image.crop((round(x - 22 * scale), max(0, round(y - 10 * scale)),
                       min(image.width, round(x + 203 * scale)),
                       min(image.height, round(y + 10 * scale))))


def quest_title_key(preview, scale):
    from PIL import Image

    normalized = preview.resize((round(preview.width / scale), 20),
                                Image.Resampling.BILINEAR).convert('RGB')
    title = normalized.crop((0, 4, 125, 16))
    return hex(int(''.join('1' if (r > 135 and b > 130 and min(r, b) - g > 34)
                           or (r > 160 and g > 95 and g - b > 50) else '0'
                           for r, g, b in _colors(title)), 2))


def same_quest_title(first, second):
    try:
        a, b = int(first, 16), int(second, 16)
    except (TypeError, ValueError):
        return False
    return a > 0 and b > 0 and (a ^ b).bit_count() <= (a | b).bit_count() * 0.25


def quest_reward_pending(image, point, scale):
    from PIL import Image, ImageChops

    x, y = point
    bounds = tuple(round(value) for value in
                   (x - 18 * scale, y + 18 * scale, x + 176 * scale, y + 32 * scale))
    region = image.crop(bounds).convert('RGB').resize((194, 14), Image.Resampling.BILINEAR)
    red, green, blue = region.split()
    white = ImageChops.darker(ImageChops.darker(red, green), blue)
    ink = white.point([ord('1') if value >= 180 else ord('0') for value in range(256)]).tobytes()
    for dy in range(3):
        for dx in range(3):
            sample = [int(ink[row * 194 + dx:row * 194 + dx + 192], 2)
                      for row in range(dy, dy + len(_REWARD_PROMPT))]
            overlap = sum((a & b).bit_count() for a, b in zip(sample, _REWARD_PROMPT))
            union = sum((a | b).bit_count() for a, b in zip(sample, _REWARD_PROMPT))
            if overlap >= 0.70 * union:
                return True
    return False


def quest_at_objective(image, point, scale):
    """Recognize the isolated 0m label at the right end of a quest row."""
    from PIL import Image, ImageChops

    x, y = point
    right = min(image.width, round(x + 220 * scale))
    left = round(x + 150 * scale)
    if right - left < 24 * scale:
        return False
    label = image.crop((left, round(y - 5 * scale), right, round(y + 7 * scale)))
    label = label.convert('RGB').resize((round(label.width / scale), 12), Image.Resampling.BILINEAR)
    r, g, b = label.split()
    pixels = ImageChops.darker(ImageChops.darker(r, g), b).point(
        [int(v >= 185) for v in range(256)]).load()
    zero_meters = (
        '####.............', '#..##............', '#...#..#########.',
        '#...#..##..##..##', '#...#..##..##..##', '#...#..#...##..##',
        '##.##..#...##..##', '.###...#...##..##',
    )
    for col in range(5, label.width - 16):
        for row in range(4):
            if sum(pixels[col - dx, row + dy] for dx in range(1, 6) for dy in range(8)) > 2:
                continue
            differences = sum(pixels[col + dx, row + dy] != (pixel == '#')
                              for dy, line in enumerate(zero_meters) for dx, pixel in enumerate(line))
            if differences <= 4:
                return True
    return False


def read_quest_tracker(hwnd):
    image, _ = capture_quest_tracker(hwnd)
    rows = find_quest_rows(image)
    titles = {slot: quest_title_key(quest_title_preview(image, *match), match[1])
              for slot, match in rows.items()}
    rewards = {slot for slot, match in rows.items() if quest_reward_pending(image, *match)}
    return titles, rewards
