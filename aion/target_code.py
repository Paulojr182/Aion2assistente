"""Read-only validation of reviewed target updater and visibility code."""
import struct
from .metadata_templates import InstructionTemplate
from .reflection_fields import ReflectionError


def pattern(item):
    operands=[tuple(pair) for pair in item['operands']]
    operands.extend((offset,offset+4) for offset in item['private_operands']+item['absolute_table_operands'])
    return InstructionTemplate(item['name'],item['old_rva'],bytes.fromhex(item['expected']),tuple(sorted(operands))).pattern()


def check_code(read,image_size,rva,item,resource_layout=None):
    length=len(bytes.fromhex(item['expected']))
    if not 0<=rva<=image_size-length:raise ReflectionError('Função de alvo fora da imagem.')
    raw=read(rva,length)
    if type(raw) is not bytes or not pattern(item).fullmatch(raw):
        raise ReflectionError('Código nativo do alvo mudou: '+item['name'])
    for offset,end in item['operands']:
        target=rva+end+struct.unpack_from('<i',raw,offset)[0]
        if not 0<=target<image_size:raise ReflectionError('Referência de código de alvo fora da imagem.')
    for offset in item['private_operands']:
        if resource_layout is None or struct.unpack_from('<i',raw,offset)[0]!=resource_layout['private_offset']:
            raise ReflectionError('Acesso privado do alvo diverge da leitura validada de HP/MP.')
    for offset in item['absolute_table_operands']:
        if resource_layout is None or struct.unpack_from('<I',raw,offset)[0]!=resource_layout['source_rva']+498:
            raise ReflectionError('Tabela de seleção de recursos do alvo diverge.')
    return raw


def called_rva(raw,rva,item,old_target):
    calls=[(offset,end) for offset,end,target in item['calls'] if target==old_target]
    if len(calls)!=1:raise ReflectionError('Ligação entre funções nativas de alvo ambígua.')
    offset,end=calls[0]
    return rva+end+struct.unpack_from('<i',raw,offset)[0]


def check_updater(schema,updater,definitions,resource_layout):
    process=schema.process;records={item['name']:item for item in definitions}
    vtable=struct.unpack('<Q',schema.read(updater['address'],8))[0]
    if not process.base<=vtable<=process.base+process.image_size-712:
        raise ReflectionError('Vtable de recursos do alvo fora da imagem.')
    tick=struct.unpack('<Q',schema.read(vtable+704,8))[0]-process.base
    read=lambda rva,size:schema.read(process.base+rva,size)
    raw=check_code(read,process.image_size,tick,records['tick'])
    wrapper=called_rva(raw,tick,records['tick'],records['wrapper']['old_rva'])
    raw=check_code(read,process.image_size,wrapper,records['wrapper'])
    source=called_rva(raw,wrapper,records['wrapper'],records['source']['old_rva'])
    check_code(read,process.image_size,source,records['source'],resource_layout)
    if source+2378!=resource_layout['source_rva']:
        raise ReflectionError('Getter do alvo não contém o acesso de recursos validado.')
    return True


def recover_visibility(process,definitions):
    records={item['name']:item for item in definitions};item=records['visible'];needle=pattern(item)
    radius=4194304;length=len(bytes.fromhex(item['expected']))
    low=max(0,item['old_rva']-radius);high=min(process.image_size,item['old_rva']+radius+length)
    candidates=set();carry=b'';previous=None;attempted=0
    for position in range(low//4096*4096,high,4096):
        size=min(4096,process.image_size-position);attempted+=size
        if attempted>8396800:raise ReflectionError('Orçamento de busca de visibilidade excedido.')
        try:raw=process.read(process.base+position,size)
        except OSError:carry=b'';previous=None;continue
        if type(raw) is not bytes or len(raw)!=size:raise ReflectionError('Leitura de código incompleta.')
        if previous!=position:carry=b''
        joined=carry+raw;origin=position-len(carry)
        for match in needle.finditer(joined):
            candidate=origin+match.start()
            if abs(candidate-item['old_rva'])<=radius:candidates.add(candidate)
        carry=joined[-(length-1):];previous=position+size
    if len(candidates)!=1:raise ReflectionError('Predicado de visibilidade ausente ou ambíguo.',details={'candidate_count':len(candidates)})
    rva=candidates.pop();read=lambda offset,size:process.read(process.base+offset,size)
    raw=check_code(read,process.image_size,rva,item)
    reference=called_rva(raw,rva,item,records['slate_reference']['old_rva'])
    check_code(read,process.image_size,reference,records['slate_reference'])
    return {'visible_rva':rva,'slate_reference_rva':reference,'native_visibility_validated':True}


def widget_visibility(schema,widget,layout,definitions):
    records={item['name']:item for item in definitions};process=schema.process
    read=lambda rva,size:schema.read_code(process.base+rva,size)
    raw=check_code(read,process.image_size,layout['visible_rva'],records['visible'])
    if called_rva(raw,layout['visible_rva'],records['visible'],records['slate_reference']['old_rva'])!=layout['slate_reference_rva']:
        raise ReflectionError('Getter de Slate mudou.')
    check_code(read,process.image_size,layout['slate_reference_rva'],records['slate_reference'])
    owner=next((item for item in schema.registry.ancestry(widget) if item['name']=='Widget'),None)
    if owner is None or owner['size']<288:raise ReflectionError('Widget não possui as referências revisadas.')
    for offset,label in ((272,'primary'),(256,'fallback')):
        slate,control=struct.unpack('<QQ',schema.read(widget['address']+offset,16))
        if not slate or not control:continue
        count=struct.unpack('<i',schema.read(control+8,4))[0]
        if not 0<=count<=1000000:raise ReflectionError('Contagem compartilhada de Slate inválida.')
        if count==0:continue
        flags=schema.read(slate+380,1)[0]
        return {'locally_visible':bool(flags&25),'slate_source':label,'native_predicate_validated':True}
    return {'locally_visible':False,'slate_source':'absent','native_predicate_validated':True}


def widget_availability(schema,widget,layout,definitions):
    """Original ActionSnapshot.visible reads flags/enabled at Slate + 380/381."""
    result=widget_visibility(schema,widget,layout,definitions)
    if result['slate_source']=='absent':return {**result,'locally_enabled':False,'available':False}
    offset=272 if result['slate_source']=='primary' else 256
    slate,control=struct.unpack('<QQ',schema.read(widget['address']+offset,16))
    if not slate or not control:raise ReflectionError('Referência Slate mudou durante a disponibilidade.')
    flags,enabled=schema.read(slate+380,2)
    if enabled not in (0,1):raise ReflectionError('Habilitação Slate não é booleana.')
    if bool(flags&25)!=result['locally_visible']:
        raise ReflectionError('Visibilidade Slate mudou durante a leitura.')
    return {**result,'locally_enabled':bool(enabled),'available':bool(result['locally_visible'] and enabled)}
