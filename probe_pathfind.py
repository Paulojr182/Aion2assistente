"""Somente leitura: mapeia os componentes de rota do personagem (auto-move do mapa).

Uso (jogo aberto, personagem logado, como administrador):
  python probe_pathfind.py            -> lista campos/funções dos componentes
  python probe_pathfind.py --watch 20 -> grava bytes dos componentes a cada 0,5 s
                                         por 20 s; durante esse tempo, abra o mapa e
                                         clique com o botão direito num ponto.
Nenhuma ação é enviada ao jogo.
"""
import argparse, json, struct, sys, time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from aion.macro_reader import MacroReader
from aion.native_player import ObjectRegistry, observe_player
from aion.reflection_fields import ReflectionSchema

COMPONENTS = ('CacheAionPathFindComponent', 'CachedPathFollowingComponent',
              'MoveTrajectoryComponent', 'NavPathRenderComp')
OUT = BASE / 'native_analysis' / 'pathfind_probe.json'


def find_pid():
    import subprocess, ctypes as C, ctypes.wintypes as W
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq AION2.exe', '/FO', 'CSV', '/NH'],
                         capture_output=True, text=True).stdout
    pids = [int(parts[1]) for parts in ([x.strip('"') for x in l.split('","')] for l in out.splitlines())
            if len(parts) > 1 and parts[0].lower() == 'aion2.exe']
    if not pids:
        raise SystemExit('AION2.exe não está aberto.')
    user32 = C.WinDLL('user32'); windowed = {}

    @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def visit(handle, _):
        owner = W.DWORD(); user32.GetWindowThreadProcessId(handle, C.byref(owner))
        if owner.value in pids and user32.IsWindowVisible(handle):
            r = W.RECT(); user32.GetWindowRect(handle, C.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            windowed[owner.value] = max(area, windowed.get(owner.value, 0))
        return True
    user32.EnumWindows(visit, 0)
    print('Processos AION2.exe:', pids, '| com janela:', list(windowed))
    if not windowed:
        raise SystemExit('Nenhum AION2.exe com janela visível; informe --pid.')
    return max(windowed, key=windowed.get)


def describe(process, registry, schema, class_addr):
    fields, funcs, chain, seen, cur = [], [], [], set(), class_addr
    while cur and cur not in seen and len(seen) < 32:
        seen.add(cur)
        info = registry.class_info(cur)
        chain.append(info['name'])
        try:
            for f in schema.own_fields(cur, info['size'], struct.unpack_from('<Q', info['raw'], 80)[0]):
                fields.append({'class': info['name'], 'name': f.name, 'kind': f.kind,
                               'offset': f.offset, 'size': f.count * f.stride})
        except Exception as exc:
            fields.append({'class': info['name'], 'error': str(exc)})
        try:
            child, cseen = struct.unpack_from('<Q', info['raw'], 72)[0], set()
            while child and child not in cseen and len(cseen) < 512:
                cseen.add(child)
                obj = registry.object(child)
                if obj.get('class_name') == 'Function':
                    name = registry.names.resolve(struct.unpack_from('<I', process.read(child, 40), 24)[0])
                    funcs.append({'class': info['name'], 'name': name, 'address': child})
                child = struct.unpack_from('<Q', process.read(child + 32, 8))[0]
        except Exception as exc:
            funcs.append({'class': info['name'], 'error': str(exc)})
        cur = info['parent']
    return chain, fields, funcs


def components(reader):
    with reader.process(33554432) as process:
        player = observe_player(process, reader.roots)
        if not player.get('player_present'):
            raise SystemExit('Entre no personagem.')
        registry = ObjectRegistry(process, reader.roots)
        schema = ReflectionSchema(registry, max_bytes=2097152)
        pawn = registry.object(player['roles']['player']['address'])
        found = {}
        for name in COMPONENTS:
            try:
                obj = schema.linked(pawn, 'AionCharacter', name)
            except Exception as exc:
                found[name] = {'error': str(exc)}
                continue
            if obj is None:
                found[name] = None
                continue
            chain, fields, funcs = describe(process, registry, schema, obj['class_address'])
            found[name] = {'address': obj['address'], 'class': obj['class_name'],
                           'class_size': obj['class_size'], 'hierarchy': chain,
                           'fields': fields, 'functions': funcs}
        return found


def watch(reader, found, seconds):
    targets = {n: (v['address'], min(v['class_size'], 4096)) for n, v in found.items()
               if isinstance(v, dict) and 'address' in v}
    samples, end = [], time.monotonic() + seconds
    print(f'Gravando por {seconds}s: abra o mapa e clique com o botão direito num ponto...')
    while time.monotonic() < end:
        with reader.process(33554432) as process:
            samples.append({'t': time.monotonic(),
                            **{n: process.read(a, s).hex() for n, (a, s) in targets.items()}})
        time.sleep(0.5)
    diffs = {}
    for n in targets:
        first = bytes.fromhex(samples[0][n])
        changed = set()
        for s in samples[1:]:
            raw = bytes.fromhex(s[n])
            changed.update(i // 8 * 8 for i in range(len(raw)) if raw[i] != first[i])
        diffs[n] = sorted(changed)
    return samples, diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pid', type=int)
    ap.add_argument('--watch', type=float, default=0)
    a = ap.parse_args()
    reader = MacroReader(BASE, a.pid or find_pid(), None)
    found = components(reader)
    result = {'game_actions_executed': False, 'components': found}
    if a.watch > 0:
        samples, diffs = watch(reader, found, a.watch)
        result['changed_offsets'] = diffs
        result['samples'] = samples
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), 'utf-8')
    for n, v in found.items():
        if isinstance(v, dict) and 'functions' in v:
            print(n, v['class'], len(v['fields']), 'campos', len(v['functions']), 'funções')
    print('Salvo em', OUT)


if __name__ == '__main__':
    main()
