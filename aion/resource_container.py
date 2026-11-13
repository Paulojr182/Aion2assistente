"""Bounded, consistent read of the recovered 84-byte byte-key resource map.

The caller must independently validate the container's provenance/layout.
No actor-private offset is assumed here.
"""
import struct


class ResourceContainerError(ValueError):
    pass


def capture_resources(read,address,*,max_slots=65536,max_bytes=1048576):
    if type(max_slots) is not int or not 0<=max_slots<=65536:
        raise ResourceContainerError('Limite de slots inválido.')
    if type(max_bytes) is not int or not 168<=max_bytes<=1048576:
        raise ResourceContainerError('Orçamento inválido.')
    def valid_range(start,size):
        if type(start) is not int or type(size) is not int or size<0 or start<65536 or start+size>0x800000000000:
            raise ResourceContainerError('Intervalo fora da memória canônica de usuário.')
    valid_range(address,84)
    used=0;journal=[];ranges=[(address,address+84)]
    def exact(start,size):
        nonlocal used
        valid_range(start,size)
        if size>max_bytes-used:raise ResourceContainerError('Orçamento de leitura esgotado.')
        used+=size;raw=read(start,size)
        if type(raw) is not bytes or len(raw)!=size:raise ResourceContainerError('Leitura incompleta ou mutável.')
        return raw
    def external(start,size):
        if not size:return b''
        valid_range(start,size)
        if any(start<end and begin<start+size for begin,end in ranges):
            raise ResourceContainerError('Armazenamentos sobrepostos.')
        ranges.append((start,start+size));raw=exact(start,size);journal.append((start,raw));return raw
    header=exact(address,84)
    u64=lambda offset:struct.unpack_from('<Q',header,offset)[0]
    i32=lambda offset:struct.unpack_from('<i',header,offset)[0]
    data,slots,capacity=u64(8),i32(16),i32(20)
    bit_pointer,bit_count=u64(40),i32(48)
    free_head,free=i32(56),i32(60)
    bucket_pointer,buckets=u64(72),i32(80)
    if not 0<=free<=slots<=capacity<=4194304 or slots>max_slots:
        raise ResourceContainerError('Contagens de slots inválidas.')
    live=slots-free
    if live>256 or bit_count!=slots:raise ResourceContainerError('Contagem de bits ou chaves inválida.')
    if buckets<0 or buckets>65536 or (buckets and buckets&(buckets-1)) or (live and not buckets):
        raise ResourceContainerError('Quantidade de buckets inválida.')
    if (not free and free_head!=-1) or (free and not 0<=free_head<slots):
        raise ResourceContainerError('Cabeça da lista livre inválida.')
    if not bit_pointer and slots>128:raise ResourceContainerError('Bits inline excedidos.')
    if not bucket_pointer and buckets>1:raise ResourceContainerError('Buckets inline excedidos.')
    bit_bytes=((slots+31)//32)*4
    planned=84+(bit_bytes if bit_pointer else 0)+slots*24+(buckets*4 if bucket_pointer else 0)
    if planned*2>max_bytes:raise ResourceContainerError('Leitura e reverificação excedem o orçamento.')
    bits=external(bit_pointer,bit_bytes) if bit_pointer else header[24:24+bit_bytes]
    entries=external(data,slots*24)
    raw_buckets=external(bucket_pointer,buckets*4) if bucket_pointer else header[64:64+buckets*4]
    occupied={index for index in range(slots) if bits[index//8]&(1<<(index%8))}
    if len(occupied)!=live or (free and free_head in occupied):
        raise ResourceContainerError('Bits de ocupação incompatíveis com contagens/lista livre.')
    values={};visited=set()
    for bucket in range(buckets):
        index=struct.unpack_from('<i',raw_buckets,bucket*4)[0]
        while index!=-1:
            if not 0<=index<slots or index not in occupied or index in visited:
                raise ResourceContainerError('Cadeia aponta para slot inválido, livre ou repetido.')
            visited.add(index);offset=index*24;key=entries[offset]
            value=struct.unpack_from('<Q',entries,offset+8)[0]
            following,stored=struct.unpack_from('<ii',entries,offset+16)
            if stored!=bucket or key&(buckets-1)!=bucket or key in values:
                raise ResourceContainerError('Chave, hash ou bucket incompatível.')
            values[key]=value;index=following
    if visited!=occupied:raise ResourceContainerError('Slots ocupados não alcançáveis.')
    for start,raw in journal:
        if exact(start,len(raw))!=raw:raise ResourceContainerError('Armazenamento mudou durante a leitura.')
    if exact(address,84)!=header:raise ResourceContainerError('Cabeçalho mudou durante a leitura.')
    return values
