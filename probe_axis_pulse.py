"""Validate, or explicitly send, one bounded normal Move_Forward/Move_Right axis pulse.

Without --execute only the guarded native validation runs (no game function is called).
With --execute: value is delivered repeatedly for --duration-ms and ALWAYS followed by
a 0.0 delivery (retried), even on errors. No retries of the pulse itself, no desktop input.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import math
from pathlib import Path
import time
from aion.macro_reader import MacroReader
from aion.native_flight import observe_flight_state
from aion.native_axis_packet import make_axis_packet
from aion.background_probe import probe_thread


def find_window(pid):
    """Largest visible top-level window owned by pid (read-only enumeration)."""
    user32 = C.WinDLL('user32')
    found = []

    @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def visit(handle, _):
        owner = W.DWORD()
        user32.GetWindowThreadProcessId(handle, C.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(handle):
            rect = W.RECT()
            user32.GetWindowRect(handle, C.byref(rect))
            found.append(((rect.right - rect.left) * (rect.bottom - rect.top), handle))
        return True

    user32.EnumWindows(visit, 0)
    if not found:
        raise ValueError('Janela do jogo não encontrada; informe --window.')
    return max(found)[1]


def position(reader):
    with reader.process(2097152) as process:
        state = observe_flight_state(process, reader.roots, with_position=True)
    if not state['position'].get('position_observed'):
        raise ValueError('Posição indisponível.')
    return state


def send(reader, window, base, artifact, bindings, axis, value, execute, stop=False):
    packet = make_axis_packet(bindings, session=reader.session, axis=axis, value=value, execute=execute, stop=stop)
    with reader.process(8388608) as process:
        return probe_thread(process, window, base / 'native' / artifact['filename'], artifact['sha256'],
                            packet=packet, protocol='axis')


def axis_state(reader, input_address):
    """Read-only dump of the per-action axis map the handler writes (this+0x5D0)."""
    import struct
    with reader.process(2097152) as process:
        storage, count = struct.unpack('<Qi', process.read(input_address + 0x5D0, 12))
        free = struct.unpack('<i', process.read(input_address + 0x604, 4))[0]
        entries = []
        if storage and 0 < count <= 128:
            raw = process.read(storage, count * 16)
            for index in range(count):
                key, value, nxt = raw[index * 16], struct.unpack_from('<f', raw, index * 16 + 4)[0], struct.unpack_from('<i', raw, index * 16 + 8)[0]
                entries.append({'index': index, 'key': key, 'value': value, 'next': nxt})
        return {'count': count, 'free': free, 'entries': entries}


def run(base, pid, window, *, axis='Move_Forward', value=1.0, duration_ms=300, execute=False):
    if not 50 <= duration_ms <= 1000 or not 0.0 < value <= 1.0:
        raise ValueError('Duração (50-1000 ms) ou valor (0-1] fora do limite do teste.')
    reader = MacroReader(base, pid, window)
    snapshot = reader.snapshot()
    if (snapshot.player_health <= 0 or snapshot.macro_active
            or snapshot.target is not None and snapshot.target.eligible and snapshot.target.health > 0):
        raise ValueError('Personagem morto, macro ou alvo de combate ativo.')
    bindings = reader.bindings(axis)
    artifact = reader.load('native/probe_build.json')['artifacts']['axis_pulse']
    before = position(reader)
    input_address = bindings['frame_objects']['input_target']['address']
    result = {'axis': axis, 'value': value, 'duration_ms': duration_ms, 'execute_requested': execute,
              'before': before, 'deliveries': [], 'stop_deliveries': [], 'approved_in_game': False}
    validation = send(reader, window, base, artifact, bindings, axis, 0.0, False)
    result['validation'] = validation
    if validation['probe_result'] != 0:
        result['ok'] = False
        return result
    if not execute:
        result['ok'] = validation['axis_binding_validated']
        return result
    result['axis_state_before'] = axis_state(reader, input_address)
    started = time.monotonic()
    try:
        while (time.monotonic() - started) * 1000 < duration_ms:
            reply = send(reader, window, base, artifact, bindings, axis, value, True)
            result['deliveries'].append({'result': reply['probe_result'], 'dispatched': reply['axis_dispatched']})
            if len(result['deliveries']) in (1, 4):
                result.setdefault('axis_state_during', []).append(axis_state(reader, input_address))
            if not reply['axis_dispatched']:
                result['error'] = 'Entrega não confirmada; interrompendo.'
                break
            time.sleep(0.03)
    finally:
        for _ in range(3):
            try:
                reply = send(reader, window, base, artifact, bindings, axis, 0.0, True)
                result['stop_deliveries'].append({'result': reply['probe_result'], 'dispatched': reply['axis_dispatched']})
                reply = send(reader, window, base, artifact, bindings, axis, 0.0, True, stop=True)
                result['stop_deliveries'].append({'result': reply['probe_result'], 'dispatched': reply['axis_dispatched'],
                                                  'phase': 'canceled'})
                if reply['axis_dispatched']:
                    result['stop_sent'] = True
                    break
            except (OSError, ValueError, RuntimeError) as exc:
                result['stop_deliveries'].append({'error': str(exc)})
            time.sleep(0.05)
    result['axis_state_after_stop'] = axis_state(reader, input_address)
    time.sleep(0.4)
    after = position(reader)
    time.sleep(0.4)
    settled = position(reader)
    if (after['session'] != before['session'] or after['player_identity'] != before['player_identity']):
        result['error'] = 'Sessão ou personagem mudou.'
    p0, p1, p2 = (item['position']['position'] for item in (before, after, settled))
    result['horizontal_displacement'] = math.dist(p0[:2], p2[:2])
    result['vertical_displacement'] = p2[2] - p0[2]
    result['final_speed_engine_units_per_second'] = math.dist(p1, p2) / 0.4
    result['stop_verified'] = result['final_speed_engine_units_per_second'] < 1.0
    result['approved_in_game'] = bool(result.get('stop_sent') and result['stop_verified'] and 'error' not in result
                                      and result['horizontal_displacement'] > 1.0 and len(result['deliveries']) > 0)
    result['ok'] = result['approved_in_game']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--window', type=int, default=None)
    parser.add_argument('--axis', choices=('Move_Forward', 'Move_Right'), default='Move_Forward')
    parser.add_argument('--value', type=float, default=1.0)
    parser.add_argument('--duration-ms', type=int, default=300)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {'execute_requested': args.execute, 'approved_in_game': False, 'ok': False}
    try:
        result = run(Path(__file__).resolve().parent, args.pid, args.window or find_window(args.pid), axis=args.axis, value=args.value,
                     duration_ms=args.duration_ms, execute=args.execute)
    except (OSError, ValueError, RuntimeError) as exc:
        result.update(ok=False, error_type=type(exc).__name__, error=str(exc))
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf8')
    print(json.dumps({key: value for key, value in result.items() if key not in ('before', 'validation')},
                     ensure_ascii=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
