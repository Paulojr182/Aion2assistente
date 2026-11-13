"""Bounded byte-to-byte contextual action map from the authenticated resolver.

Native resolver: 12-byte sparse nodes, key byte 0, value byte 1, next i32 at
4; the hash uses the input byte. The caller validates resolver instructions.
"""
import struct
from .name_pool import pointer
from .reflection_fields import ReflectionError


def read_contextual_action_map(read, root, *, max_bytes=65536):
    pointer(root)
    if type(max_bytes) is not int or not 160 <= max_bytes <= 1048576:
        raise ReflectionError('Orçamento do resolvedor contextual inválido.')
    journal = {}
    used = 0

    def capture(address, size):
        nonlocal used
        pointer(address)
        if not 0 < size <= max_bytes-used:
            raise ReflectionError('Orçamento do resolvedor contextual excedido.')
        used += size
        raw = read(address,size)
        if type(raw) is not bytes or len(raw) != size:
            raise ReflectionError('Leitura do resolvedor contextual incompleta.')
        key=address,size
        if key in journal and journal[key] != raw:
            raise ReflectionError('Resolvedor mudou durante a leitura.')
        journal[key] = raw
        return raw

    header = capture(root,80)
    storage, slots, capacity = struct.unpack_from('<Qii',header)
    bits_external, bits_count, bits_capacity = struct.unpack_from('<Qii',header,32)
    free = struct.unpack_from('<i',header,52)[0]
    external, bucket_count = struct.unpack_from('<Qi',header,64)
    if (not 0 <= free <= slots <= capacity <= 256 or bits_count != slots
            or not slots <= bits_capacity <= 256 or bits_capacity % 32
            or not bits_external and bits_capacity > 128):
        raise ReflectionError('Slots ou bitmap do resolvedor inválidos.')
    bitmap = capture(bits_external or root+16,((slots+31)//32)*4) if slots else b''
    allocated = {i for i in range(slots) if bitmap[i//8] & (1 << (i%8))}
    if len(allocated) != slots-free:
        raise ReflectionError('Contagem do resolvedor diverge do bitmap.')
    seen=set()
    result={}
    if allocated:
        pointer(storage)
        if (not 0 < bucket_count <= 512 or bucket_count & (bucket_count-1)
                or not external and bucket_count > 1):
            raise ReflectionError('Buckets do resolvedor inválidos.')
        heads = struct.unpack('<'+'i'*bucket_count,capture(external or root+56,bucket_count*4))
        for bucket, index in enumerate(heads):
            while index != -1:
                if index in seen or index not in allocated:
                    raise ReflectionError('Ciclo ou slot livre no resolvedor contextual.')
                seen.add(index)
                node = capture(storage+index*12,12)
                key,value = node[:2]
                if key in result or key & (bucket_count-1) != bucket:
                    raise ReflectionError('Ação duplicada ou bucket contextual incorreto.')
                result[key] = value
                index = struct.unpack_from('<i',node,4)[0]
    if seen != allocated:
        raise ReflectionError('Hash contextual não reconcilia os slots alocados.')
    for (address,size),expected in journal.items():
        if size > max_bytes-used:
            raise ReflectionError('Orçamento de reconferência contextual excedido.')
        used += size
        if read(address,size) != expected:
            raise ReflectionError('Resolvedor contextual mudou antes da publicação.')
    return result
