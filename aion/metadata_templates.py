"""Bounded relocation-aware metadata discovery, not gameplay compatibility.

Templates and operand positions are extracted data from the original program.
Only rel32 displacements are variable; every other instruction byte must match.
"""
from dataclasses import dataclass
import re,struct


class MetadataRecoveryError(ValueError):
    def __init__(self,message,*,partial=None):
        super().__init__(message)
        self.partial=partial


@dataclass(frozen=True)
class InstructionTemplate:
    name: str
    old_rva: int
    expected: bytes
    operands: tuple[tuple[int,int], ...]
    radius: int = 262144

    def pattern(self):
        parts=[];cursor=0
        for offset,end in self.operands:
            if offset<cursor or end<offset+4 or end>len(self.expected):
                raise MetadataRecoveryError('Operandos relativos inválidos.')
            parts.extend((re.escape(self.expected[cursor:offset]),b'.{4}'))
            cursor=offset+4
        parts.append(re.escape(self.expected[cursor:]))
        if not self.expected:
            raise MetadataRecoveryError('Assinatura vazia.')
        return re.compile(b''.join(parts),re.S)

    def targets(self,code,rva,image_size):
        if not self.pattern().fullmatch(code):
            raise MetadataRecoveryError('Instruções divergem fora dos operandos relativos.')
        values=tuple(rva+end+struct.unpack_from('<i',code,offset)[0] for offset,end in self.operands)
        if any(not 0 <= value < image_size for value in values):
            raise MetadataRecoveryError('Alvo relativo fora da imagem.')
        return values


def metadata_roots(targets,anchors,image_size,*,require_default=True):
    global_=targets['global_weak'];local=targets['local_weak'];names=targets['name_pool_accessor']
    if len(global_)!=4 or len(local)!=2 or len(names)!=5:
        raise MetadataRecoveryError('Quantidade de referências inesperada.')
    if (global_[0]!=global_[1]+4 or global_[2]!=local[0] or global_[3]!=local[1]
            or global_[2]!=global_[3]+20 or names[1]!=names[2]
            or names[0]!=names[4] or names[0]!=names[1]-43):
        raise MetadataRecoveryError('Referências independentes de metadados não concordam.')
    roots={'name_pool_rva':names[1],'registry_rva':global_[3]-16,'global_weak_rva':global_[1]}
    if require_default:
        roots['default_accessor_rva']=anchors['default_accessor']
    if any(not 0 <= value < image_size for value in roots.values()):
        raise MetadataRecoveryError('Raiz fora da imagem.')
    return roots


def diagnostic_read_roots(targets,image_size):
    """Derive only read locations; never declares an old gameplay profile valid.

    The original 43-byte guard placement changed in the observed current client.
    Agreement between both registry references and both name-pool LEAs remains
    mandatory. Runtime allocator/sentinel checks must follow these code checks.
    """
    global_=targets['global_weak'];local=targets['local_weak'];names=targets['name_pool_accessor']
    if (len(global_)!=4 or len(local)!=2 or len(names)!=5 or
            global_[0]!=global_[1]+4 or global_[2]!=local[0] or global_[3]!=local[1]
            or global_[2]!=global_[3]+20 or names[1]!=names[2] or names[0]!=names[4]):
        raise MetadataRecoveryError('Referências de leitura não concordam.')
    if names[0]==names[1] or names[1]%8:
        raise MetadataRecoveryError('Guard e tabela de nomes não possuem identidades distintas alinhadas.')
    roots={'name_pool_rva':names[1],'registry_rva':global_[3]-16,'global_weak_rva':global_[1]}
    if any(not 0<=value<image_size for value in roots.values()):
        raise MetadataRecoveryError('Raiz de leitura fora da imagem.')
    return roots


def recover_templates(reader,image_size,templates,*,max_bytes=2097152,page_size=4096):
    required={'global_weak','local_weak','name_pool_accessor','default_accessor'}
    if len(templates)!=4 or {t.name for t in templates}!=required:
        raise MetadataRecoveryError('São necessários os quatro modelos independentes de metadados.')
    if (type(image_size) is not int or image_size<=0 or type(max_bytes) is not int
            or not 0<max_bytes<=33554432):
        raise MetadataRecoveryError('Limites de imagem ou busca inválidos.')
    for template in templates:
        if (type(template.radius) is not int or not 0<=template.radius<=4194304
                or not 0<=template.old_rva<image_size
                or len(template.expected)>image_size-template.old_rva):
            raise MetadataRecoveryError('Modelo fora dos limites de busca.')
    requested=0;records={};targets={};anchors={}
    for template in templates:
        pattern=template.pattern();matches={};unavailable=0;carry=b'';previous_end=None
        if template.radius==0:
            chunks=((template.old_rva,len(template.expected)),)
        else:
            low=max(0,template.old_rva-template.radius)
            high=min(image_size,template.old_rva+template.radius+len(template.expected))
            start=(low//page_size)*page_size
            chunks=((p,min(page_size,image_size-p)) for p in range(start,high,page_size))
        for position,size in chunks:
            requested+=size
            if requested>max_bytes:
                raise MetadataRecoveryError('Orçamento de busca esgotado.')
            try:
                data=reader(position,size)
            except OSError:
                unavailable+=1;carry=b'';previous_end=None;continue
            if not isinstance(data,bytes) or len(data)!=size:
                raise MetadataRecoveryError('Leitura de código incompleta.')
            if previous_end!=position:
                carry=b''
            joined=carry+data;origin=position-len(carry)
            for match in pattern.finditer(joined):
                candidate=origin+match.start()
                if abs(candidate-template.old_rva)<=template.radius:
                    matches[candidate]=match.group()
            carry=joined[-(len(template.expected)-1):] if len(template.expected)>1 else b''
            previous_end=position+size
        if len(matches)!=1:
            partial={'anchors':dict(records),'requested_bytes':requested,'relative_targets':dict(targets)}
            if {'global_weak','local_weak','name_pool_accessor'}<=targets.keys():
                try:
                    partial['independent_read_roots']=metadata_roots(targets,anchors,image_size,require_default=False)
                except MetadataRecoveryError as exc:
                    partial['consistency_error']=str(exc)
                    try:
                        partial['diagnostic_read_roots']=diagnostic_read_roots(targets,image_size)
                        partial['observed_pool_guard_distance']=targets['name_pool_accessor'][1]-targets['name_pool_accessor'][0]
                    except MetadataRecoveryError as read_error:
                        partial['read_consistency_error']=str(read_error)
            raise MetadataRecoveryError(f'{template.name}: {len(matches)} candidatos; nenhuma escolha automática.',partial=partial)
        rva,code=next(iter(matches.items()))
        requested+=len(code)
        if requested>max_bytes:
            raise MetadataRecoveryError('Orçamento de busca esgotado.')
        if reader(rva,len(code))!=code:
            raise MetadataRecoveryError('Código mudou durante a verificação.')
        targets[template.name]=template.targets(code,rva,image_size)
        anchors[template.name]=rva
        records[template.name]={'old_rva':template.old_rva,'new_rva':rva,
                                'relative_targets':targets[template.name],'unavailable_pages':unavailable}
    return {'roots':metadata_roots(targets,anchors,image_size),'anchors':records,
            'requested_bytes':requested,'gameplay_compatibility_validated':False,
            'profile_promoted':False,'game_actions_executed':False}
