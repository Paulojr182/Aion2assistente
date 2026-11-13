"""Closed, bounded game-thread movement packets. No desktop input."""
import ctypes as C
import math
import struct
from .native_macro_cycle import MacroCyclePacket, NativeObjectRef, make_collect_packet, _unsigned
from .native_player import ObjectRegistry, observe_player
from .reflection_fields import ReflectionError

MOVEMENT_THUNK_BYTES = bytes.fromhex(
    '48895c241848896c2420565741564883ec60488bda4c8bf1e803fe6cfd488b4320488d2d78123e0833ff4885c07427'
    '440fb600488bd3488b4b1848ffc048894320418bc04c8d442420ff54c500488db388000000eb62488db38800000048'
    '8b0e488b41184889068b4138480fbae0087326488b8380000000483908740c0f1f00488b401048390875f7488b4008'
    '4889433848897b40eb2148634144488d5424204c8b43284903c04c89434048894338488b01ff90100100000f57c0f3'
    '0f11842488000000e8b73a6cfd488b43204885c074210fb6084c8d84248800000048ffc0488bd3488943208bc1488b'
    '4b18ff54c500eb5b488b0e488b41184889068b4138480fbae0087323488b83800000004839087409488b4010483908'
    '75f7488b40084889433848897b40eb2448634144488d9424880000004c8b43284903c04c89434048894338488b01ff'
    '901001000089bc2480000000e8f6f369fd488b43204885c074210fb6084c8d84248000000048ffc0488bd348894320'
    '8bc1488b4b18ff54c500eb5b488b0e488b41184889068b4138480fbae0087323488b83800000004839087409488b40'
    '1048390875f7488b40084889433848897b40eb2448634144488d9424800000004c8b43284903c04c89434048894338'
    '488b01ff9010010000488b4320488d5424400f104424204885c0498bcef20f104c2430f30f10942488000000400f95'
    'c74803f80f2944244083bc24800000000048897b20498b06410f95c1f20f114c2450ff90600800004c8d5c2460498b'
    '5b30498b6b38498be3415e5f5ec3'
)
# rel32 operands (call/lea rip) shift between game builds; compared as wildcards.
MOVEMENT_THUNK_REL = (25, 36, 197, 342)


def _matches_masked(raw, expected, rel):
    if len(raw) != len(expected):
        return False
    skip = {o + k for o in rel for k in range(4)}
    return all(i in skip or raw[i] == expected[i] for i in range(len(expected)))


MOVEMENT_BODY_BYTES = bytes.fromhex(
    '488bc448897010574883ec700f2970e8410fb6f1440f2940c80f28f248895808488bf9488b01488bdaff9030070000'
    'f20f104b08488bc8f20f105310f30f5af6440f28c6f2440f5903488b9c24800000004885c07432488b00488d542420'
    '0f28c6f2440f11442420f20f59c1440fb6c6f20f59f2f20f11442428f20f11742430ff9020060000eb5c0f297c2450'
    '0f28fef20f59f9f20f59f24084f67510488b07488bcfff908808000084c07532f2440f588788030000f20f58bf9003'
    '0000f20f58b798030000f2440f118788030000f20f11bf90030000f20f11b7980300000f287c2450488bb424880000'
    '000f28742460440f284424404883c4705fc3'
)


def observe_movement_profile(process, roots, *, function_hint=21278):
    player = observe_player(process, roots)
    if not player['player_present']:
        raise ReflectionError('Entre no personagem para validar o perfil de movimento.')
    registry = ObjectRegistry(process, roots)
    pawn = registry.object(player['roles']['player']['address'])
    klass_addr = pawn['class_address']
    pawn_class = None
    for _ in range(32):
        if not klass_addr:
            break
        info = registry.class_info(klass_addr)
        if info['name'] == 'Pawn':
            pawn_class = registry.object(klass_addr)
            break
        klass_addr = info['parent']
    if pawn_class is None:
        raise ReflectionError('Classe Pawn ausente na hierarquia do personagem.')
    func = None
    try:
        addr, _ = registry.slot(function_hint)
        candidate = registry.object(addr)
        header = process.read(addr, 40)
        name = registry.names.resolve(struct.unpack_from('<I', header, 24)[0])
        owner = struct.unpack_from('<Q', header, 32)[0]
        if candidate['class_name'] == 'Function' and name == 'AddMovementInput' and owner == pawn_class['address']:
            func = candidate
    except (ValueError, OSError):
        pass
    if func is None:
        for slot_idx in range(1, min(registry.count, 65536)):
            try:
                addr, _ = registry.slot(slot_idx)
                header = process.read(addr, 40)
                if struct.unpack_from('<Q', header, 32)[0] != pawn_class['address']:
                    continue
                if registry.names.resolve(struct.unpack_from('<I', header, 24)[0]) != 'AddMovementInput':
                    continue
                candidate = registry.object(addr)
                if candidate['class_name'] == 'Function':
                    func = candidate
                    break
            except (ValueError, OSError):
                continue
    if func is None or func['class_size'] < 224:
        raise ReflectionError('Função Pawn.AddMovementInput ausente ou inválida.')
    thunk_addr = struct.unpack('<Q', process.read(func['address']+216, 8))[0]
    thunk_rva = thunk_addr - process.base
    if (not 0 <= thunk_rva <= process.image_size - len(MOVEMENT_THUNK_BYTES)
            or not _matches_masked(process.read(thunk_addr, len(MOVEMENT_THUNK_BYTES)), MOVEMENT_THUNK_BYTES, MOVEMENT_THUNK_REL)):
        raise ReflectionError('Thunk nativo de AddMovementInput mudou.')
    vtable = struct.unpack('<Q', process.read(pawn['address'], 8))[0]
    if not process.base <= vtable <= process.base + process.image_size - 0x868:
        raise ReflectionError('Vtable do Pawn fora da imagem.')
    movement_addr = struct.unpack('<Q', process.read(vtable+0x860, 8))[0]
    movement_rva = movement_addr - process.base
    if (not 0 <= movement_rva <= process.image_size - len(MOVEMENT_BODY_BYTES)
            or process.read(movement_addr, len(MOVEMENT_BODY_BYTES)) != MOVEMENT_BODY_BYTES):
        raise ReflectionError('Corpo virtual de AddMovementInput mudou.')
    registry.verify()
    return {
        'session': process.session,
        'function': {'address': func['address'], 'index': func['index'], 'serial': func['serial']},
        'owner_class': {'address': pawn_class['address'], 'index': pawn_class['index'], 'serial': pawn_class['serial']},
        'thunk_rva': thunk_rva,
        'movement_rva': movement_rva,
    }


class MovementPacket(MacroCyclePacket):
    _fields_ = [('function', NativeObjectRef), ('owner_class', NativeObjectRef),
                ('thunk_rva', C.c_uint64), ('movement_rva', C.c_uint64),
                ('direction', C.c_double*3), ('duration_ms', C.c_uint32), ('execute', C.c_uint32)]


def make_movement_packet(bindings, profile, *, session, direction=(0., 0., 0.),
                         duration_ms=100, execute=False):
    if type(execute) is not bool or profile.get('session') != session:
        raise ReflectionError('Perfil de movimento pertence a outra sessão.')
    if type(duration_ms) is not int or not 16 <= duration_ms <= 1500:
        raise ReflectionError('Pulso de movimento deve durar entre 16 e 1500 ms.')
    if (len(direction) != 3 or any(type(v) not in (int, float) or not math.isfinite(v) for v in direction)
            or abs(direction[2]) > 1e-9 or sum(v*v for v in direction) > 1.000001
            or execute and sum(v*v for v in direction) < 0.000001):
        raise ReflectionError('Direção de movimento terrestre inválida.')
    base = make_collect_packet(bindings, session=session)
    packet = MovementPacket()
    C.memmove(C.byref(packet), C.byref(base), C.sizeof(base))
    for label in ('function', 'owner_class'):
        ref = profile[label]
        setattr(packet, label, NativeObjectRef(_unsigned(ref['address'], 64, label, minimum=65536),
                                               _unsigned(ref['index'], 32, label),
                                               _unsigned(ref['serial'], 32, label)))
    packet.thunk_rva = _unsigned(profile['thunk_rva'], 64, 'Thunk')
    packet.movement_rva = _unsigned(profile['movement_rva'], 64, 'Movimento')
    packet.direction[:] = direction
    packet.duration_ms = duration_ms
    packet.execute = int(execute)
    return packet
