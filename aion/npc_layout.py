"""Recover NPC info/cache members by repeated operands in reviewed code."""
import struct
from .metadata_templates import InstructionTemplate
from .reflection_fields import ReflectionError


def npc_pattern(definition):
    operands=[tuple(pair) for pair in definition['operands']]
    operands.extend((offset,offset+4) for offsets in definition['fields'].values() for offset in offsets)
    return InstructionTemplate(definition['name'],definition['old_rva'],bytes.fromhex(definition['expected']),tuple(sorted(operands))).pattern()


def validate_npc_layout(read,image_size,rva,definition):
    size=len(bytes.fromhex(definition['expected']))
    if not 0<=rva<=image_size-size:raise ReflectionError('Código de cache de NPC fora da imagem.')
    raw=read(rva,size)
    if type(raw) is not bytes or not npc_pattern(definition).fullmatch(raw):raise ReflectionError('Código do cache de NPC mudou.')
    for offset,end in definition['operands']:
        target=rva+end+struct.unpack_from('<i',raw,offset)[0]
        if not 0<=target<image_size:raise ReflectionError('Referência de cache de NPC fora da imagem.')
    values={}
    for role,offsets in definition['fields'].items():
        operands={struct.unpack_from('<i',raw,offset)[0] for offset in offsets}
        if len(operands)!=1:raise ReflectionError('Referências ao membro de NPC não concordam: '+role)
        values[role]=operands.pop()
    if set(values)!={'info','cache','cache_control'} or not 1824<=values['info']<values['cache']<=65520 or any(value%8 for value in values.values()) or values['cache_control']!=values['cache']+8:
        raise ReflectionError('Extensão dos membros de NPC inválida.')
    return {**values,'code_rva':rva,'native_npc_layout_validated':True}


def recover_npc_layout(process,definition):
    needle=npc_pattern(definition);radius=4194304;length=len(bytes.fromhex(definition['expected']))
    low=max(0,definition['old_rva']-radius);high=min(process.image_size,definition['old_rva']+radius+length)
    candidates=set();carry=b'';previous=None;attempted=0
    for position in range(low//4096*4096,high,4096):
        size=min(4096,process.image_size-position);attempted+=size
        if attempted>8400896:raise ReflectionError('Orçamento da busca de cache NPC excedido.')
        try:raw=process.read(process.base+position,size)
        except OSError:carry=b'';previous=None;continue
        if type(raw) is not bytes or len(raw)!=size:raise ReflectionError('Leitura de código de NPC incompleta.')
        if previous!=position:carry=b''
        joined=carry+raw;origin=position-len(carry)
        for match in needle.finditer(joined):
            candidate=origin+match.start()
            if abs(candidate-definition['old_rva'])<=radius:candidates.add(candidate)
        carry=joined[-(length-1):];previous=position+size
    if len(candidates)!=1:raise ReflectionError('Cache de NPC ausente ou ambíguo.',details={'candidate_count':len(candidates)})
    return validate_npc_layout(lambda rva,size:process.read(process.base+rva,size),process.image_size,candidates.pop(),definition)
