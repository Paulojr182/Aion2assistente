"""Bounded consistent reader for the observed u8/u8 input-action map.

Returns raw entries, not a claim that a gameplay macro is running. The caller
must independently validate the owning object and the layout's native code.
"""
import struct
from .name_pool import pointer


class InputActionMapError(ValueError):
    pass


def read_input_action_map(read, address, *, max_bytes=65536):
    if type(max_bytes) is not int or not 160 <= max_bytes <= 65536:
        raise InputActionMapError('Orçamento de leitura inválido.')
    return {key:value[0] for key,value in _read_map(read,address,max_bytes=max_bytes,
            entry_size=12,value_offset=1,value_size=1,next_offset=4,bucket_offset=8).items()}


def read_input_subscription_map(read, address, *, max_bytes=131072):
    if type(max_bytes) is not int or not 160 <= max_bytes <= 131072:
        raise InputActionMapError('Orçamento de leitura inválido.')
    return _read_map(read,address,max_bytes=max_bytes,entry_size=48,value_offset=8,
                     value_size=32,next_offset=40,bucket_offset=44)


def read_input_definition_map(read,address,*,max_bytes=196608):
    if type(max_bytes) is not int or not 160<=max_bytes<=196608:
        raise InputActionMapError('Orçamento de leitura inválido.')
    return _read_map(read,address,max_bytes=max_bytes,entry_size=72,value_offset=8,
                     value_size=56,next_offset=64,bucket_offset=68)


def control_key_hash(key):
    first,second=key
    return ((first>>2)+0x9e3779b9+second+(first<<6))&0xffffffff ^ first


def read_input_control_map(read,address,*,max_bytes=131072):
    if type(max_bytes) is not int or not 160<=max_bytes<=131072:
        raise InputActionMapError('Orçamento de leitura inválido.')
    return _read_map(read,address,max_bytes=max_bytes,entry_size=32,value_offset=8,
                     value_size=16,next_offset=24,bucket_offset=28,key_size=2)


def read_input_route_map(read,address,*,max_bytes=131072):
    if type(max_bytes) is not int or not 160<=max_bytes<=131072:
        raise InputActionMapError('Orçamento de leitura inválido.')
    return _read_map(read,address,max_bytes=max_bytes,entry_size=32,value_offset=8,
                     value_size=16,next_offset=24,bucket_offset=28)


def _read_map(read,address,*,max_bytes,entry_size,value_offset,value_size,next_offset,bucket_offset,key_size=1):
    used = 0
    journal = []
    regions = [(address, address + 80)]

    def exact(start, size):
        nonlocal used
        pointer(start)
        if size <= 0 or start + size > 0x800000000000 or size > max_bytes - used:
            raise InputActionMapError('Leitura fora do limite.')
        used += size
        result = read(start, size)
        if type(result) is not bytes or len(result) != size:
            raise InputActionMapError('Leitura incompleta.')
        return result

    def external(start, size):
        if not size:
            return b''
        if any(start < end and begin < start + size for begin, end in regions):
            raise InputActionMapError('Armazenamentos sobrepostos.')
        regions.append((start, start + size))
        result = exact(start, size)
        journal.append((start, result))
        return result

    header = exact(address, 80)
    data, slots, capacity = struct.unpack_from('<Qii', header)
    bits_pointer = struct.unpack_from('<Q', header, 32)[0]
    bit_count, bit_capacity, free_head, free = struct.unpack_from('<iiii', header, 40)
    buckets_pointer = struct.unpack_from('<Q', header, 64)[0]
    bucket_count = struct.unpack_from('<i', header, 72)[0]
    if not 0 <= free <= slots <= capacity <= 1024 or slots - free > 256 or bit_count != slots:
        raise InputActionMapError('Contagens de ações inválidas.')
    if not slots <= bit_capacity <= 0x100000 or not bits_pointer and bit_capacity > 128:
        raise InputActionMapError('Contagens de bits inválidas.')
    if bucket_count < 0 or bucket_count > 1024 or bucket_count and bucket_count & (bucket_count - 1):
        raise InputActionMapError('Contagem de buckets inválida.')
    if slots > free and not bucket_count or not buckets_pointer and bucket_count > 1:
        raise InputActionMapError('Armazenamento de buckets inválido.')
    if not free and free_head != -1 or free and not 0 <= free_head < slots:
        raise InputActionMapError('Cabeça da lista livre inválida.')
    bit_bytes = ((slots + 31) // 32) * 4
    bits = external(bits_pointer, bit_bytes) if bits_pointer else header[16:16 + bit_bytes]
    entries = external(data, slots * entry_size)
    buckets = external(buckets_pointer, bucket_count * 4) if buckets_pointer else header[56:56 + bucket_count * 4]
    occupied = {i for i in range(slots) if bits[i // 8] & (1 << (i % 8))}
    if len(occupied) != slots - free or free and free_head in occupied:
        raise InputActionMapError('Ocupação inconsistente.')
    visited = set()
    result = {}
    for bucket in range(bucket_count):
        index = struct.unpack_from('<i', buckets, bucket * 4)[0]
        while index != -1:
            if index not in occupied or index in visited:
                raise InputActionMapError('Ciclo ou índice de ação inválido.')
            visited.add(index)
            start=index*entry_size
            key=entries[start] if key_size==1 else tuple(entries[start:start+2])
            value=entries[start+value_offset:start+value_offset+value_size]
            following=struct.unpack_from('<i',entries,start+next_offset)[0]
            stored_bucket=struct.unpack_from('<i',entries,start+bucket_offset)[0]
            hashed=key if key_size==1 else control_key_hash(key)
            if stored_bucket != bucket or hashed & (bucket_count - 1) != bucket or key in result:
                raise InputActionMapError('Chave ou hash de ação inconsistente.')
            result[key] = value
            index = following
    if visited != occupied:
        raise InputActionMapError('Ações ocupadas não alcançáveis.')
    for start, original in journal:
        if exact(start, len(original)) != original:
            raise InputActionMapError('O mapa mudou durante a leitura.')
    if exact(address, 80) != header:
        raise InputActionMapError('O cabeçalho mudou durante a leitura.')
    return result
