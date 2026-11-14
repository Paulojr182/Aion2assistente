"""Sincroniza o bot com a versao atual do AION 2 (botao "Sincronizar com a versao do jogo").

Somente leitura no jogo: procura cada assinatura de codigo (bytes com operandos mascarados) perto do
endereco antigo e grava os enderecos novos em:
  - native/game_offsets.txt   (lido pelas DLLs em tempo de execucao; nada e recompilado)
  - *templates*.json          (campo old_rva; copia de seguranca em data/versoes/<tamanho_antigo>/)
  - macro_state_templates.json (image_size)
Nada e alterado se alguma assinatura obrigatoria nao for encontrada de forma unica.
Resultado em data/sync_result.json.
"""
import glob, json, re, shutil, sys, time, traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from aion.native_memory import ReadOnlyProcess
from aion.game_image import image_path

WINDOW = 64 * 1048576          # busca +-64 MiB ao redor do endereco antigo
STEP = 4 * 1048576
OFFSETS = BASE / 'native' / 'game_offsets.txt'
RESULT = BASE / 'data' / 'sync_result.json'
# Essenciais: enderecos usados diretamente (DLLs, macro, marcacao do mouse). Os demais *templates*.json
# sao reencontrados pelo proprio bot a cada conexao (busca com raio); aqui so recebem uma dica melhor.
ESSENTIAL_FILES = {'macro_state_templates.json', 'input_gate_templates.json'}

# Planejamento legado: pode ser desativado sem bloquear ataque/coleta atuais.
OPTIONAL_NATIVE = {('auto_move_fingerprints.h', 'stop_move')}

def essential(sig):
    if (sig['source'], sig.get('name')) in OPTIONAL_NATIVE:
        return False
    return sig['source'].endswith('_fingerprints.h') or sig['source'] in ESSENTIAL_FILES


def find_pid():
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    snap = k.CreateToolhelp32Snapshot(2, 0)
    class PE(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', ctypes.c_long), ('dwFlags', wintypes.DWORD),
                    ('szExeFile', ctypes.c_wchar * 260)]
    e = PE(); e.dwSize = ctypes.sizeof(PE); found = []
    ok = k.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        if e.szExeFile.lower() == 'aion2.exe': found.append(e.th32ProcessID)
        ok = k.Process32NextW(snap, ctypes.byref(e))
    k.CloseHandle(snap)
    if not found: raise SystemExit('AION 2 nao esta aberto. Abra o jogo e entre no personagem.')
    try:
        from probe_pathfind import find_pid as game_pid      # prefere o processo com janela
        return game_pid()
    except Exception:
        return found[0]


# ---------- assinaturas ----------
def header_signatures():
    """Le native/*_fingerprints.h: X_rva + X_bytes + (X_rel de inicios de 4 bytes | X_mask)."""
    out = []
    for f in sorted((BASE / 'native').glob('*_fingerprints.h')):
        text = f.read_text('utf-8')
        rvas = dict(re.findall(r'constexpr std::uint64_t (\w+)_rva = (\d+);', text))
        for name, rva in rvas.items():
            m = re.search(r'constexpr std::uint8_t ' + name + r'_bytes\[\] = \{([^}]*)\};', text)
            if not m:
                out.append({'source': f.name, 'name': name, 'old_rva': int(rva), 'data_global': True}); continue
            raw = bytes(int(x, 16) for x in m.group(1).split(','))
            wild = set()
            mm = re.search(r'constexpr std::uint8_t ' + name + r'_mask\[\] = \{([^}]*)\};', text)
            if mm:
                wild = {i for i, x in enumerate(mm.group(1).split(',')) if int(x, 16) == 0}
            else:
                r = re.search(r'constexpr std::uint16_t ' + name + r'_rel\[\] = \{([^}]*)\};', text)
                if r:
                    for start in (int(x) for x in r.group(1).split(',') if x.strip()):
                        wild.update(range(start, start + 4))
            out.append({'source': f.name, 'name': name, 'old_rva': int(rva), 'raw': raw, 'wild': wild})
    return out


def json_signatures():
    out = []
    for f in sorted(glob.glob(str(BASE / '*templates*.json'))):
        name = Path(f).name
        data = json.loads(Path(f).read_text('utf-8'))
        items = data if isinstance(data, list) else data.get('templates', [])
        for i, t in enumerate(items):
            if isinstance(t, dict) and 'old_rva' in t and 'expected' in t:
                wild = set()
                for a, b in t.get('operands', []):
                    wild.update(range(a, b))
                out.append({'source': name, 'index': i, 'name': t.get('name'), 'old_rva': t['old_rva'],
                            'raw': bytes.fromhex(t['expected']), 'wild': wild, 'derive': t.get('derive')})
    return out


def regex(sig):
    raw, wild = sig['raw'], sig['wild']; parts = []; i = 0
    while i < len(raw):
        if i in wild:
            j = i
            while j < len(raw) and j in wild: j += 1
            parts.append(b'.{%d}' % (j - i)); i = j
        else:
            parts.append(re.escape(raw[i:i + 1])); i += 1
    return re.compile(b''.join(parts), re.S)


def main():
    started = time.time()
    result = {'ok': False, 'started_at': started, 'game_actions_executed': False}
    try:
        pid = int(sys.argv[1]) if len(sys.argv) > 1 else find_pid()
        image = image_path(pid)
        print('Jogo:', image)
        sigs = header_signatures() + json_signatures()
        with ReadOnlyProcess(pid, str(image), max_bytes=1 << 31) as p:
            size = p.image_size; result['image_size'] = size
            cache = {}
            def chunk(start):
                if start not in cache:
                    try: cache[start] = p.read(p.base + start, STEP + 4096)
                    except Exception:
                        parts = []
                        for off in range(0, STEP + 4096, 65536):
                            n = min(65536, STEP + 4096 - off)
                            try: parts.append(p.read(p.base + start + off, n))
                            except Exception: parts.append(bytes(n))
                        cache[start] = b''.join(parts)
                return cache[start]
            def read(rva, n):
                try: return p.read(p.base + rva, n)
                except Exception: return b''
            for s in sigs:
                if s.get('data_global'):
                    continue
                rx = regex(s); old = s['old_rva']; n = len(s['raw'])
                here = read(old, n)
                if here and rx.fullmatch(here):
                    s['new_rva'] = old; continue
                found = []
                lo = max(0, (old - WINDOW) // STEP * STEP); hi = min(size - n, old + WINDOW)
                for start in range(lo, hi, STEP):
                    for m in rx.finditer(chunk(start)):
                        if m.start() < STEP and start + m.start() not in found:
                            found.append(start + m.start())
                    if len(found) > 1: break
                s['found'] = found[:3]
                if len(found) == 1: s['new_rva'] = found[0]
            # derivados (enderecos de dados lidos de instrucoes [rip+disp])
            derived = {}
            for s in sigs:
                for key, d in (s.get('derive') or {}).items():
                    if 'new_rva' in s:
                        code = read(s['new_rva'], d['end'])
                        disp = int.from_bytes(code[d['disp_at']:d['disp_at'] + 4], 'little', signed=True)
                        derived[key] = s['new_rva'] + d['end'] + disp
        missing = [s for s in sigs if not s.get('data_global') and 'new_rva' not in s]
        blocking = [s for s in missing if essential(s)]
        moved = [s for s in sigs if s.get('new_rva') not in (None, s['old_rva'])]
        moved_essential = [s for s in moved if essential(s)]
        result.update(signatures=len(sigs), moved=len(moved), missing=[(s['source'], s['name'], s.get('found')) for s in missing])
        print(f'Assinaturas: {len(sigs)} | essenciais que mudaram: {len(moved_essential)} | dicas atualizadas: {len(moved) - len(moved_essential)} '
              f'| essenciais nao encontradas: {len(blocking)} | outras sem dica (o bot procura sozinho): {len(missing) - len(blocking)}')
        for s in blocking: print('  ESSENCIAL nao encontrada:', s['source'], s['name'], 'candidatos:', s.get('found'))
        result['missing_essential'] = [(s['source'], s['name'], s.get('found')) for s in blocking]
        if blocking:
            result['message'] = (f'{len(blocking)} assinatura(s) essencial(is) nao encontrada(s). Esta versao do jogo mudou demais; '
                                 'nada foi alterado. Envie data/sync_result.json para atualizar o bot.')
            print(result['message']); return 1
        # --- grava ---
        macro_path = BASE / 'macro_state_templates.json'
        old_size = json.loads(macro_path.read_text('utf-8')).get('image_size')
        backup = BASE / 'data' / 'versoes' / str(old_size)
        if old_size != size or moved_essential:
            backup.mkdir(parents=True, exist_ok=True)
            for f in list(glob.glob(str(BASE / '*templates*.json'))) + ([str(OFFSETS)] if OFFSETS.exists() else []):
                if not (backup / Path(f).name).exists(): shutil.copy2(f, backup / Path(f).name)
        by_file = {}
        for s in sigs:
            if 'index' in s and s.get('new_rva') is not None and (essential(s) or old_size != size):
                by_file.setdefault(s['source'], {})[s['index']] = s['new_rva']
        for name, changes in by_file.items():
            path = BASE / name; data = json.loads(path.read_text('utf-8'))
            items = data if isinstance(data, list) else data.get('templates', [])
            dirty = False
            for i, rva in changes.items():
                if items[i]['old_rva'] != rva: items[i]['old_rva'] = rva; dirty = True
            if name == 'macro_state_templates.json' and data.get('image_size') != size:
                data['image_size'] = size; dirty = True
            if dirty: path.write_text(json.dumps(data, indent=1, ensure_ascii=False), 'utf-8')
        lines = [f'# Gerado por sincronizar_versao.py em {time.strftime("%Y-%m-%d %H:%M:%S")} para imagem {size}',
                 f'expected_game_image_size={size}']
        for s in sigs:
            if s['source'].endswith('_fingerprints.h') and not s.get('data_global'):
                lines.append(f"{s['name']}_rva={s.get('new_rva', 0)}")
        same_version = old_size == size and not moved_essential
        legacy_plan_available = all(s.get('new_rva') is not None for s in sigs
                                    if (s['source'], s['name']) in OPTIONAL_NATIVE)
        for s in sigs:
            if s.get('data_global'):          # endereco de dado sem assinatura propria: so vale na versao capturada
                lines.append(f"{s['name']}_rva={s['old_rva'] if same_version and legacy_plan_available else 0}")
        for key, value in derived.items():
            lines.append(f'{key}_rva={value}')
        OFFSETS.write_text('\n'.join(lines) + '\n', encoding='ascii')
        result.update(ok=True, version_changed=old_size != size, derived=derived,
                      message=('Bot ja estava sincronizado com esta versao do jogo.' if same_version else
                               f'Sincronizado com a nova versao do jogo ({len(moved_essential)} endereco(s) atualizados). Clique em Conectar de novo.'))
        result['moved'] = len(moved_essential)
        result['disabled_native'] = [s['name'] for s in missing
                                     if (s['source'], s['name']) in OPTIONAL_NATIVE]
        if result['disabled_native']:
            result['message'] += ' Planejamento de rota legado indisponivel; ataque e coleta atuais mantidos.'
        print(result['message']); return 0
    except SystemExit as exc:
        result['message'] = str(exc); print(exc); return 1
    except Exception as exc:
        result['message'] = f'Falha na sincronizacao: {exc}'; result['detail'] = traceback.format_exc()
        print(result['detail']); return 1
    finally:
        result['seconds'] = round(time.time() - started, 1)
        RESULT.parent.mkdir(exist_ok=True)
        RESULT.write_text(json.dumps(result, indent=1, default=str), 'utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
