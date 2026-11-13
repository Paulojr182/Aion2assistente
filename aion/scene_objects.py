"""Read-only parser for the original manager's 76-byte bucket collections."""
from dataclasses import dataclass
import struct
from .name_pool import pointer

MASK=0xffffffff


class SceneReadError(ValueError):
    pass


class SceneChanged(SceneReadError):
    """Previously captured scene bytes changed before publication."""
    pass


class UnsupportedSceneKey(SceneReadError):
    pass


def combine(first,second):
    b=0x9e3779b9;a=(first+b)&MASK;c=second&MASK
    for target,shift,left in (('a',13,False),('b',8,True),('c',13,False),
                              ('a',12,False),('b',16,True),('c',5,False),
                              ('a',3,False),('b',10,True),('c',15,False)):
        if target=='a':a=((a-b-c)^((c<<shift) if left else (c>>shift)))&MASK
        elif target=='b':b=((b-c-a)^((a<<shift) if left else (a>>shift)))&MASK
        else:c=((c-a-b)^((b<<shift) if left else (b>>shift)))&MASK
    return c


@dataclass(frozen=True)
class ObjectKey:
    tag: int
    payload: int
    extra: int

    def hash_u32(self):
        payload_hash=((self.payload&MASK)+23*(self.payload>>32))&MASK
        return combine(payload_hash,combine(self.tag,self.extra))


def decode_key(raw):
    if type(raw) is not bytes or len(raw)!=24:raise SceneReadError('Chave deve ter 24 bytes imutáveis.')
    tag=raw[0];payload=struct.unpack_from('<Q',raw,8)[0];extra=raw[16]
    if tag not in (1,2) or payload==0 or (tag==1 and payload>MASK):
        raise UnsupportedSceneKey('Tipo ou conteúdo de chave não suportado.')
    return ObjectKey(tag,payload,extra)


@dataclass(frozen=True)
class SceneNode:
    slot: int
    bucket: int
    address: int
    key: ObjectKey
    weak_index: int
    weak_serial: int
    following: int


@dataclass
class Collection:
    header_address: int
    nodes: tuple[SceneNode,...]
    journal: dict
    bytes_read: int
    max_bytes: int
    skipped_keys: int = 0

    def verify(self,read):
        for (address,size),expected in self.journal.items():
            if size>self.max_bytes-self.bytes_read:raise SceneReadError('Orçamento de verificação esgotado.')
            self.bytes_read+=size
            if read(address,size)!=expected:raise SceneChanged('Coleção mudou durante a observação.')


def read_collection(read,header_address,*,max_nodes=1024,max_bytes=65536,known_keys_only=False):
    pointer(header_address)
    if type(max_nodes) is not int or not 0<=max_nodes<=1024 or type(max_bytes) is not int or not 76<=max_bytes<=65536:
        raise SceneReadError('Limites da coleção inválidos.')
    journal={};used=0
    def observe(address,size):
        nonlocal used
        pointer(address)
        if size<=0 or size>max_bytes-used:raise SceneReadError('Orçamento da coleção esgotado.')
        used+=size;data=read(address,size)
        if type(data) is not bytes or len(data)!=size:raise SceneReadError('Leitura de coleção incompleta.')
        key=(address,size)
        if key in journal and journal[key]!=data:raise SceneReadError('Estrutura mudou durante a leitura.')
        journal[key]=data;return data
    header=observe(header_address,76)
    allocated=struct.unpack_from('<I',header,8)[0]
    free=struct.unpack_from('<I',header,52)[0]
    if free>allocated or allocated>65536:raise SceneReadError('Contagem de slots inválida.')
    if free==allocated:
        result=Collection(header_address,(),journal,used,max_bytes);result.verify(read);return result
    count=struct.unpack_from('<I',header,72)[0]
    external=struct.unpack_from('<Q',header,64)[0]
    if count==0 or count>128 or count&(count-1) or (not external and count>1):
        raise SceneReadError('Buckets fora do limite ou sem armazenamento válido.')
    bucket_address=external or header_address+56
    if bucket_address&3:raise SceneReadError('Buckets desalinhados.')
    buckets=struct.unpack('<'+'i'*count,observe(bucket_address,count*4))
    storage=pointer(struct.unpack_from('<Q',header)[0])
    if storage&7:raise SceneReadError('Armazenamento de nós desalinhado.')
    seen=set();keys=set();nodes=[]
    for bucket,index in enumerate(buckets):
        while index!=-1:
            if not 0<=index<allocated:raise SceneReadError('Índice fora dos slots alocados.')
            if index in seen:raise SceneReadError('Ciclo ou slot repetido na coleção.')
            if len(seen)>=max_nodes:raise SceneReadError('Limite de nós da coleção excedido.')
            seen.add(index);address=storage+index*40;raw=observe(address,40)
            following=struct.unpack_from('<i',raw,32)[0]
            if following < -1 or following>=allocated:raise SceneReadError('Próximo índice inválido.')
            try:
                key=decode_key(raw[:24])
            except UnsupportedSceneKey:
                if not known_keys_only:raise
                index=following
                continue
            if key in keys:raise SceneReadError('Chave semântica duplicada.')
            keys.add(key)
            if key.hash_u32()&(count-1)!=bucket:raise SceneReadError('Hash da chave diverge do bucket.')
            weak_index,serial=struct.unpack_from('<iI',raw,24)
            nodes.append(SceneNode(index,bucket,address,key,weak_index,serial,following))
            index=following
    if len(seen)!=allocated-free:raise SceneReadError('Quantidade de nós alcançáveis diverge da contagem ativa.')
    result=Collection(header_address,tuple(nodes),journal,used,max_bytes,len(seen)-len(nodes))
    result.verify(read)
    return result
