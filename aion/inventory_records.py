"""Bounded inventory-domain parser reconstructed from native profile evidence.

Evidence: inventory_interaction_constants.json and inventory_functions.json,
retail_inventory_probe.read_item_domain; native lookup multiplies key high32
by 23 before adding low32. Callers must verify layout code before use.
Quantity is mutable and uses an independent read port; matching samples do
not make the entire inventory an atomic snapshot or classify storage roles.
"""
from dataclasses import dataclass
import struct
from .name_pool import pointer


class InventoryReadError(ValueError):
    pass


@dataclass(frozen=True)
class InventoryRecord:
    domain: int
    slot: int
    instance_id: int
    definition_id: int
    quantity: int


ITEM_DOMAINS = ((1, 1896), (2, 2056), (4, 1976), (8, 2216), (128, 2136))


def read_item_domain(read, read_quantity, root, domain, *, max_slots=8192, max_bytes=2097152,
                     quantity_journal=None):
    pointer(root)
    if (not callable(read_quantity) or domain not in dict(ITEM_DOMAINS)
            or type(domain) is not int or type(max_slots) is not int
            or not 0 < max_slots <= 8192 or type(max_bytes) is not int
            or not 160 <= max_bytes <= 16777216):
        raise InventoryReadError('Domínio, limites ou leitor de quantidade inválidos.')
    journal = {}
    used = 0

    def capture(address, size):
        nonlocal used
        pointer(address)
        if not 0 < size <= max_bytes-used:
            raise InventoryReadError('Orçamento de inventário excedido.')
        used += size
        raw = read(address, size)
        if type(raw) is not bytes or len(raw) != size:
            raise InventoryReadError('Leitura de inventário incompleta.')
        key = address, size
        if key in journal and journal[key] != raw:
            raise InventoryReadError('Inventário mudou durante a leitura.')
        journal[key] = raw
        return raw

    header = capture(root, 80)
    storage, slots, capacity = struct.unpack_from('<Qii', header)
    bits_external, bits_count, bits_capacity = struct.unpack_from('<Qii', header, 32)
    free = struct.unpack_from('<i', header, 52)[0]
    external, bucket_count = struct.unpack_from('<Qi', header, 64)
    if (not 0 <= free <= slots <= capacity <= max_slots
            or bits_count != slots or not slots <= bits_capacity <= max_slots
            or bits_capacity % 32 or not bits_external and bits_capacity > 128):
        raise InventoryReadError('Alocação de slots/bitmap inválida.')
    active = slots-free
    bitmap = capture(bits_external or root+16, ((slots+31)//32)*4) if slots else b''
    allocated = {i for i in range(slots) if bitmap[i//8] & (1 << (i % 8))}
    if len(allocated) != active:
        raise InventoryReadError('Bitmap e contagem ativa do inventário divergem.')
    rows = []
    samples = []
    seen = set()
    instances = set()
    if active:
        pointer(storage)
        if (not 0 < bucket_count <= 16384 or bucket_count & (bucket_count-1)
                or not external and bucket_count > 1):
            raise InventoryReadError('Buckets de inventário inválidos.')
        heads = struct.unpack('<'+'i'*bucket_count, capture(external or root+56, bucket_count*4))
        for bucket, index in enumerate(heads):
            while index != -1:
                if index in seen or index not in allocated:
                    raise InventoryReadError('Ciclo ou slot livre no hash de inventário.')
                seen.add(index)
                instance, record, control, following = struct.unpack('<QQQi', capture(storage+index*32, 28))
                hash_value = ((instance >> 32)*23 + (instance & 0xffffffff)) & 0xffffffff
                if not instance or instance in instances or hash_value & (bucket_count-1) != bucket:
                    raise InventoryReadError('Instância duplicada ou bucket incorreto.')
                instances.add(instance)
                pointer(record)
                if control:pointer(control)
                identity, definition = struct.unpack('<QI', capture(record, 12))
                if identity != instance or not definition:
                    raise InventoryReadError('Identidade de item não corresponde ao índice.')
                quantity = read_quantity(record+16, 8)
                if type(quantity) is not bytes or len(quantity) != 8:
                    raise InventoryReadError('Amostra de quantidade incompleta.')
                count = struct.unpack('<q', quantity)[0]
                if count < 0:
                    raise InventoryReadError('Quantidade de item negativa.')
                samples.append((record+16, quantity))
                rows.append(InventoryRecord(domain, index, instance, definition, count))
                index = following
    if seen != allocated:
        raise InventoryReadError('Slots alocados não foram reconciliados pelo hash.')
    for (address, size), expected in journal.items():
        if size > max_bytes-used:
            raise InventoryReadError('Orçamento de verificação excedido.')
        used += size
        if read(address, size) != expected:
            raise InventoryReadError('Inventário mudou antes da publicação.')
    for address, expected in samples:
        if read_quantity(address, 8) != expected:
            raise InventoryReadError('Quantidade mudou entre as amostras.')
    if quantity_journal is not None:quantity_journal.extend(samples)
    return tuple(sorted(rows, key=lambda row: row.instance_id))


def aggregate_items(records):
    """Aggregate confirmed records; callers choose the independently classified domains."""
    totals = {}
    instances = set()
    for row in records:
        if (not isinstance(row, InventoryRecord) or row.instance_id in instances
                or row.definition_id <= 0 or row.quantity < 0):
            raise InventoryReadError('Registros agregados inválidos ou instância duplicada.')
        instances.add(row.instance_id)
        totals[row.definition_id] = totals.get(row.definition_id, 0) + row.quantity
    return tuple(sorted(totals.items()))
