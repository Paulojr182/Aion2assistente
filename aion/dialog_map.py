"""Consistent read of the reflected 80-byte LoadedDialogMap layout."""
import struct
from .name_pool import pointer


class DialogMapError(ValueError):pass


def read_dialog_map(read,address,*,max_bytes=131072):
    if type(max_bytes) is not int or not 160<=max_bytes<=131072:raise DialogMapError('Orçamento inválido.')
    used=0;journal=[];regions=[(address,address+80)]
    def exact(start,size):
        nonlocal used
        pointer(start)
        if size<=0 or start+size>0x800000000000 or size>max_bytes-used:raise DialogMapError('Limite de leitura excedido.')
        used+=size;raw=read(start,size)
        if type(raw) is not bytes or len(raw)!=size:raise DialogMapError('Leitura de mapa incompleta.')
        return raw
    def external(start,size):
        if not size:return b''
        if any(start<end and begin<start+size for begin,end in regions):raise DialogMapError('Armazenamentos do mapa sobrepostos.')
        regions.append((start,start+size));raw=exact(start,size);journal.append((start,raw));return raw
    header=exact(address,80)
    data,slots,capacity=struct.unpack_from('<Qii',header)
    bits_pointer=struct.unpack_from('<Q',header,32)[0]
    bit_count,bit_capacity,free_head,free=struct.unpack_from('<iiii',header,40)
    bucket_pointer=struct.unpack_from('<Q',header,64)[0];bucket_count=struct.unpack_from('<i',header,72)[0]
    if not 0<=free<=slots<=capacity<=1024 or bit_count!=slots or not slots<=bit_capacity<=0x100000:
        raise DialogMapError('Contagens de diálogos inválidas.')
    if not bits_pointer and bit_capacity>128:raise DialogMapError('Bits inline excedidos.')
    if bucket_count<0 or bucket_count>2048 or bucket_count and bucket_count&(bucket_count-1):raise DialogMapError('Buckets inválidos.')
    if slots>free and not bucket_count or not bucket_pointer and bucket_count>1:raise DialogMapError('Armazenamento de buckets inválido.')
    if not free and free_head!=-1 or free and not 0<=free_head<slots:raise DialogMapError('Lista livre inválida.')
    bit_bytes=((slots+31)//32)*4
    bits=external(bits_pointer,bit_bytes) if bits_pointer else header[16:16+bit_bytes]
    entries=external(data,slots*32)
    buckets=external(bucket_pointer,bucket_count*4) if bucket_pointer else header[56:56+bucket_count*4]
    occupied={index for index in range(slots) if bits[index//8]&(1<<(index%8))}
    if len(occupied)!=slots-free or free and free_head in occupied:raise DialogMapError('Bits de diálogos inconsistentes.')
    seen=set();result=[]
    for bucket in range(bucket_count):
        index=struct.unpack_from('<i',buckets,bucket*4)[0]
        while index!=-1:
            if index not in occupied or index in seen:raise DialogMapError('Ciclo ou índice inválido no mapa de diálogos.')
            seen.add(index);raw=entries[index*32:(index+1)*32]
            following,stored=struct.unpack_from('<ii',raw,24)
            if stored!=bucket:raise DialogMapError('Bucket do diálogo diverge.')
            result.append((raw[:12],struct.unpack_from('<Q',raw,16)[0]));index=following
    if seen!=occupied:raise DialogMapError('Diálogos ocupados não alcançáveis.')
    for start,raw in journal:
        if exact(start,len(raw))!=raw:raise DialogMapError('Diálogos mudaram durante a leitura.')
    if exact(address,80)!=header:raise DialogMapError('Cabeçalho de diálogos mudou.')
    return tuple(result)
