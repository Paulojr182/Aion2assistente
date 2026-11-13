"""Read-only checked lookup for native table indices using u32 keys.

The caller supplies a table root whose accessor, constructor and lookup code
have been independently checked. No layout discovery or code execution here.
"""
import struct
from .name_pool import pointer


class TableReadError(ValueError):
    pass


def lookup_u32_row(read, root, key, *, expected_vtable, max_bytes=65536):
    if type(key) is not int or not 0 < key <= 0xffffffff:
        raise TableReadError('ID de tabela inválido.')
    pointer(root)
    journal = {}
    used = 0

    def capture(address, size):
        nonlocal used
        pointer(address)
        if not 0 < size <= max_bytes-used:
            raise TableReadError('Orçamento da tabela excedido.')
        used += size
        raw = read(address, size)
        if type(raw) is not bytes or len(raw) != size:
            raise TableReadError('Leitura de tabela incompleta.')
        journal[address, size] = raw
        return raw

    if struct.unpack('<Q', capture(root, 8))[0] != expected_vtable:
        raise TableReadError('Vtable da tabela não corresponde ao construtor.')
    raw = capture(root+96, 92)
    values, count, capacity = struct.unpack_from('<Qii', raw)
    storage, allocated = struct.unpack_from('<Qi', raw, 16)
    free = struct.unpack_from('<i', raw, 68)[0]
    if not 0 <= count <= capacity <= 1000000 or not 0 <= free <= allocated <= 1000000:
        raise TableReadError('Limites do índice de tabela inválidos.')
    result = None
    if free != allocated:
        external, buckets = struct.unpack_from('<Qi', raw, 80)
        if not 0 < buckets <= 1048576 or buckets & (buckets-1) or not external and buckets > 1:
            raise TableReadError('Buckets do índice inválidos.')
        bucket_base = external or root+168
        index = struct.unpack('<i', capture(bucket_base+(key & (buckets-1))*4, 4))[0]
        seen = set()
        while index != -1:
            if index in seen or not 0 <= index < allocated or len(seen) >= 1024:
                raise TableReadError('Ciclo ou índice inválido na tabela.')
            seen.add(index)
            node = capture(storage+index*16, 16)
            stored_key, value_index, index = struct.unpack_from('<Iii', node)
            if stored_key == key:
                if not 0 <= value_index < count:
                    raise TableReadError('Linha de tabela fora do array.')
                pair = capture(values+value_index*16, 16)
                row, control = struct.unpack('<QQ', pair)
                pointer(row)
                if control:
                    pointer(control)
                result = row, control
                break
    for (address, size), expected in journal.items():
        if size > max_bytes-used:
            raise TableReadError('Orçamento de verificação da tabela excedido.')
        used += size
        if read(address, size) != expected:
            raise TableReadError('Tabela mudou durante a leitura.')
    return result


def enumerate_u32_rows(read, root, *, expected_vtable, limit=4096):
    """Read value rows, then independently prove each row's key via lookup."""
    if not 0 < limit <= 65536:
        raise TableReadError('Limite de catálogo inválido.')
    raw = read(root+96, 16)
    if type(raw) is not bytes or len(raw) != 16:
        raise TableReadError('Cabeçalho de catálogo incompleto.')
    values, count, capacity = struct.unpack('<Qii', raw)
    if not 0 <= count <= capacity <= 1000000 or count > limit:
        raise TableReadError('Catálogo fora do limite.')
    rows = []
    pairs = read(values, count*16) if count else b''
    if type(pairs) is not bytes or len(pairs) != count*16:
        raise TableReadError('Array de catálogo incompleto.')
    keys = set()
    for index in range(count):
        row, control = struct.unpack_from('<QQ', pairs, index*16)
        pointer(row)
        key = struct.unpack('<I', read(row+8, 4))[0]
        if key in keys or lookup_u32_row(read, root, key, expected_vtable=expected_vtable) != (row, control):
            raise TableReadError('ID de catálogo duplicado ou não confirmado pelo índice.')
        keys.add(key)
        rows.append((key, row, control))
    if read(root+96, 16) != raw or count and read(values, count*16) != pairs:
        raise TableReadError('Catálogo mudou durante a enumeração.')
    return tuple(rows)
