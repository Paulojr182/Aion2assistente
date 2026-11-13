"""Bounded quest index/progress parser, reconstructed from native evidence.

Offsets are grounded in the selected-record lookup and progress accessors in
activity_code_templates.json. Callers must validate that code in the current
client before applying this layout. Raw state/key values are not translated
into definition IDs or completion semantics without the definition reader.
"""
from dataclasses import dataclass
import struct
from .name_pool import pointer


class QuestRecordsError(ValueError):
    pass


@dataclass(frozen=True)
class QuestProgressRecord:
    index_key: int
    record_address: int
    control_address: int
    state_byte: int
    stage_index: int
    progress: tuple[int, ...]
    definition_container: int
    definition_id: int | None = None
    step_id: int | None = None


def read_quest_records(read, system, session, *, max_nodes=1024,
                       max_objectives=64, max_bytes=262144):
    pointer(system)
    pointer(session)
    if (type(max_nodes) is not int or not 0 < max_nodes <= 4096
            or type(max_objectives) is not int or not 0 < max_objectives <= 256
            or type(max_bytes) is not int or not 256 <= max_bytes <= 2097152):
        raise QuestRecordsError('Limites da leitura de missões inválidos.')
    journal = {}
    used = 0

    def capture(address, size):
        nonlocal used
        pointer(address)
        if size <= 0 or size > max_bytes-used:
            raise QuestRecordsError('Orçamento de leitura de missões excedido.')
        used += size
        raw = read(address, size)
        if type(raw) is not bytes or len(raw) != size:
            raise QuestRecordsError('Leitura de missão incompleta.')
        key = address, size
        if key in journal and journal[key] != raw:
            raise QuestRecordsError('Missões mudaram durante a leitura.')
        journal[key] = raw
        return raw

    header = capture(system, 128)
    if struct.unpack_from('<Q', header, 32)[0] != session:
        raise QuestRecordsError('Sistema de missões pertence a outra sessão.')
    storage, allocated = struct.unpack_from('<Qi', header, 48)
    free = struct.unpack_from('<i', header, 100)[0]
    if not 0 <= free <= allocated <= 1000000 or allocated-free > max_nodes:
        raise QuestRecordsError('Contagem de registros de missão inválida.')
    records = []
    if allocated != free:
        pointer(storage)
        external, count = struct.unpack_from('<Qi', header, 112)
        if not 0 < count <= 4096 or count & (count-1) or not external and count > 1:
            raise QuestRecordsError('Buckets de missão inválidos.')
        buckets = struct.unpack('<'+'i'*count, capture(external or system+104, count*4))
        seen = set()
        keys = set()
        for bucket, index in enumerate(buckets):
            while index != -1:
                if index in seen or not 0 <= index < allocated or len(seen) >= max_nodes:
                    raise QuestRecordsError('Ciclo, índice ou limite inválido nas missões.')
                seen.add(index)
                node = capture(storage+index*32, 32)
                key = struct.unpack_from('<I', node)[0]
                record, control = struct.unpack_from('<QQ', node, 8)
                following = struct.unpack_from('<i', node, 24)[0]
                if key in keys or key & (count-1) != bucket:
                    raise QuestRecordsError('Chave de missão duplicada ou bucket incorreto.')
                keys.add(key)
                pointer(record)
                if control:
                    pointer(control)
                raw = capture(record, 72)
                stage_index = struct.unpack_from('<i', raw, 40)[0]
                progress_pointer, progress_count = struct.unpack_from('<Qi', raw, 48)
                container = struct.unpack_from('<Q', raw, 64)[0]
                if not 0 <= progress_count <= max_objectives or stage_index < -1:
                    raise QuestRecordsError('Objetivos ou etapa de missão inválidos.')
                progress = ()
                if progress_count:
                    progress = struct.unpack('<'+'I'*progress_count,
                                             capture(progress_pointer, progress_count*4))
                if container:
                    pointer(container)
                definition_id = step_id = None
                if container:
                    definition_header = capture(container, 20)
                    definition_id = struct.unpack_from('<I', definition_header)[0]
                    stages, stage_count = struct.unpack_from('<Qi', definition_header, 8)
                    if not definition_id or not 0 <= stage_count <= 1024:
                        raise QuestRecordsError('Definição de missão ou etapas inválidas.')
                    if stage_index >= 0:
                        if stage_index >= stage_count:
                            raise QuestRecordsError('Etapa atual fora da definição da missão.')
                        stage_pair = capture(stages+stage_index*16, 16)
                        stage = struct.unpack_from('<Q', stage_pair)[0]
                        step_id = struct.unpack('<I', capture(stage, 4))[0]
                        if not step_id:
                            raise QuestRecordsError('ID da etapa atual inválido.')
                records.append(QuestProgressRecord(key, record, control, raw[8],
                                                    stage_index, progress, container,
                                                    definition_id, step_id))
                index = following
        if len(seen) != allocated-free:
            raise QuestRecordsError('Contagem ativa diverge das missões alcançáveis.')
    for (address, size), expected in journal.items():
        if size > max_bytes-used:
            raise QuestRecordsError('Orçamento de verificação de missões excedido.')
        used += size
        if read(address, size) != expected:
            raise QuestRecordsError('Missões mudaram antes da publicação.')
    return tuple(sorted(records, key=lambda record: record.index_key))
