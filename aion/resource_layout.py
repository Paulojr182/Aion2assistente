"""Recover a read-only resource layout from independent native code checks."""
import re,struct
from .metadata_templates import InstructionTemplate


class ResourceLayoutError(ValueError):
    pass


def code_pattern(item):
    operands=[tuple(pair) for pair in item['operands']]
    operands.extend((offset,offset+4) for offset,_ in item.get('absolute_operands',()))
    return InstructionTemplate(item['name'],item['old_rva'],bytes.fromhex(item['expected']),tuple(sorted(operands))).pattern()


def validate_resource_code(read,image_size,source_rva,definitions):
    templates={item['name']:InstructionTemplate(item['name'],item['old_rva'],bytes.fromhex(item['expected']),
                tuple(tuple(pair) for pair in item['operands'])) for item in definitions}
    if set(templates)!={'lookup','actor_source','health','mana','mana_max_conversion'}:
        raise ResourceLayoutError('Conjunto incompleto de verificações nativas.')
    source=templates['actor_source'];targets={};records={item['name']:item for item in definitions}
    for name in ('actor_source','health','mana','mana_max_conversion'):
        template=templates[name];rva=source_rva+template.old_rva-source.old_rva
        if not 0<=rva<=image_size-len(template.expected):raise ResourceLayoutError('Código fora da imagem.')
        raw=read(rva,len(template.expected))
        if not code_pattern(records[name]).fullmatch(raw):
            # Nova versao do jogo pode deslocar o trecho dentro da mesma funcao: procura unico perto.
            found=None
            if name!='actor_source':
                hits=set()
                # perto do endereco esperado e dentro da propria funcao de origem (layout novo do jogo)
                for lo,size in ((max(0,rva-0x3000),0x6000+len(template.expected)),(source_rva,0x300)):
                    try:window=read(lo,size)
                    except Exception:window=None
                    if type(window) is bytes:
                        hits.update(lo+m.start() for m in code_pattern(records[name]).finditer(window))
                if len(hits)==1:found=hits.pop()
            if found is None:
                raise ResourceLayoutError('Corpo nativo de recursos diverge: '+name)
            rva=found;raw=read(rva,len(template.expected))
        targets[name]=tuple(rva+end+struct.unpack_from('<i',raw,offset)[0] for offset,end in template.operands)
        if any(not 0<=value<image_size for value in targets[name]):raise ResourceLayoutError('Referência relativa fora da imagem.')
        if name=='actor_source':
            absolute=records[name].get('absolute_operands')
            if absolute!=[[54,498]] or targets[name][1]!=0:
                raise ResourceLayoutError('Base ou descrição da tabela de recursos diverge.')
            table_rva=struct.unpack_from('<I',raw,54)[0]
            if table_rva!=source_rva+498:raise ResourceLayoutError('Referência absoluta não acompanha a função.')
            expected=bytes.fromhex(records[name]['jump_table_expected'])
            if len(expected)!=28 or table_rva>image_size-len(expected):raise ResourceLayoutError('Tabela de seleção inválida.')
            actual=read(table_rva,len(expected));delta=source_rva-source.old_rva
            if actual!=b''.join(struct.pack('<I',value[0]+delta) for value in struct.iter_unpack('<I',expected)):
                raise ResourceLayoutError('Destinos da tabela de seleção não concordam.')
    lookups=targets['health']+targets['mana']
    if len(lookups)!=4 or len(set(lookups))!=1:
        raise ResourceLayoutError('Leituras de HP/MP não convergem para o mesmo lookup.')
    lookup=templates['lookup'];lookup_rva=lookups[0]
    if lookup_rva>image_size-len(lookup.expected) or read(lookup_rva,len(lookup.expected))!=lookup.expected:
        raise ResourceLayoutError('Layout nativo do container não confirmado.')
    # This exact block comes from the original resource profile. Only the
    # actor-private displacement is discovered; the container offset stays 176.
    private=read(source_rva+63,14)
    if type(private) is not bytes or len(private)!=14 or private[:3]!=bytes.fromhex('488b86') or private[7:]!=bytes.fromhex('4c8d98b0000000'):
        raise ResourceLayoutError('Acesso à estrutura privada não confirmado.')
    offset=struct.unpack_from('<i',private,3)[0]
    if offset<1824 or offset>65528 or offset%8:raise ResourceLayoutError('Offset privado inválido.')
    return {'private_offset':offset,'container_offset':176,'source_rva':source_rva,'lookup_rva':lookup_rva,
            'read_layout_validated':True,'gameplay_profile_promoted':False}


def recover_resource_layout(process,definitions):
    item=next(value for value in definitions if value['name']=='actor_source')
    template=InstructionTemplate(item['name'],item['old_rva'],bytes.fromhex(item['expected']),
                                 tuple(tuple(pair) for pair in item['operands']))
    radius=4194304;low=max(0,item['old_rva']-radius);high=min(process.image_size,item['old_rva']+radius+len(template.expected))
    patterns={value['name']:code_pattern(value)
              for value in definitions if value['name'] in ('actor_source','health','mana')}
    patterns['private_access']=re.compile(bytes.fromhex('488b86')+b'.{4}'+re.escape(bytes.fromhex('4c8d98b0000000')),re.S)
    matches={name:set() for name in patterns};carry=b'';previous=None;attempted=0;unreadable=0
    for position in range((low//4096)*4096,high,4096):
        size=min(4096,process.image_size-position);attempted+=size
        if attempted>8396800:raise ResourceLayoutError('Orçamento de busca de recursos excedido.')
        try:raw=process.read(process.base+position,size)
        except OSError:carry=b'';previous=None;unreadable+=1;continue
        if type(raw) is not bytes or len(raw)!=size:raise ResourceLayoutError('Leitura de código incompleta.')
        if previous!=position:carry=b''
        joined=carry+raw;origin=position-len(carry)
        for name,pattern in patterns.items():
            for match in pattern.finditer(joined):
                candidate=origin+match.start()
                if abs(candidate-item['old_rva'])<=radius:
                    if len(matches[name])>=64 and candidate not in matches[name]:
                        raise ResourceLayoutError('Excesso de candidatos de assinatura.')
                    matches[name].add(candidate)
        carry=joined[-127:];previous=position+size
    candidates=matches['actor_source']
    if len(candidates)!=1:
        result={'read_layout_validated':False,'stage':'resource_code_anchor_unavailable_or_ambiguous',
                'candidate_count':len(candidates),'bytes_attempted':attempted,'unreadable_pages':unreadable,
                'diagnostic_anchors':{name:sorted(values) for name,values in matches.items()},
                'gameplay_profile_promoted':False}
        if len(matches['health'])==1:
            health=next(iter(matches['health']))
            source=health-82
            if 0<=source<=process.image_size-63:
                result['diagnostic_source_context_hex']=process.read(process.base+source,63).hex()
        return result
    result=validate_resource_code(lambda rva,size:process.read(process.base+rva,size),process.image_size,candidates.pop(),definitions)
    result.update(bytes_attempted=attempted,unreadable_pages=unreadable)
    return result
