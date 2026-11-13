"""Bounded immutable reads of the name-pool layout observed in the original."""
import struct


class NamePoolError(ValueError):
    def __init__(self,message,*,details=None):
        super().__init__(message);self.details=details


class NamePoolChanged(NamePoolError):
    pass


def pointer(value):
    if type(value) is not int or not 0x10000 <= value <= 0x7fffffffffff:
        raise NamePoolError('Ponteiro fora do espaço de usuário.')
    return value


class NamePoolReader:
    def __init__(self,read,pool_address,*,budget=65536,max_length=1023):
        if not callable(read) or not 1<=budget<=65536 or not 1<=max_length<=1023:
            raise NamePoolError('Leitor ou limites inválidos.')
        self.read=read;self.pool=pointer(pool_address);self.budget=budget;self.max_length=max_length

    def exact(self,address,size):
        pointer(address)
        if not 0<size<=self.budget:
            raise NamePoolError('Orçamento de nomes esgotado.')
        self.budget-=size;data=self.read(address,size)
        if type(data) is not bytes or len(data)!=size:
            raise NamePoolError('Leitura de nome incompleta.')
        return data

    def resolve(self,entry_id):
        if type(entry_id) is not int or not 0<=entry_id<=0x1fffffff:
            raise NamePoolError('Identificador de nome inválido.')
        before=self.exact(self.pool+8,8)
        block,cursor=struct.unpack('<II',before)
        if block>=8192 or cursor>131072:
            raise NamePoolError('Estado do alocador inválido.')
        entry_block=entry_id>>16;offset=(entry_id&65535)*2
        if entry_block>block:
            raise NamePoolError('Bloco de nome ainda não alocado.')
        block_pointer=pointer(struct.unpack('<Q',self.exact(self.pool+16+8*entry_block,8))[0])
        address=block_pointer+offset
        header=struct.unpack('<H',self.exact(address,2))[0]
        length=header>>6;wide=bool(header&1);size=length*(2 if wide else 1)
        allocated=(2+size+1)&~1
        if not 1<=length<=self.max_length or offset+allocated>131072:
            raise NamePoolError('Comprimento de nome inválido.')
        if entry_block==block and offset+allocated>cursor:
            raise NamePoolError('Nome fora do intervalo alocado.')
        data=self.exact(address+2,size)
        slot=self.pool+16+8*entry_block
        if (self.exact(slot,8)!=struct.pack('<Q',block_pointer)
                or self.exact(address,2+size)!=struct.pack('<H',header)+data):
            raise NamePoolChanged('Entrada de nome mudou durante a leitura.')
        after_block,after_cursor=struct.unpack('<II',self.exact(self.pool+8,8))
        if (after_block>=8192 or after_cursor>131072
                or (after_block,after_cursor)<(block,cursor)):
            raise NamePoolChanged('Alocador recuou ou ficou inválido durante a leitura.')
        try:
            value=data.decode('utf-16-le' if wide else 'utf-8','strict')
        except UnicodeError as exc:
            raise NamePoolError('Codificação do nome não foi validada.') from exc
        if '\0' in value:
            raise NamePoolError('Nome contém terminador inesperado.')
        return value


def valid_registry_limits(capacity,count,chunk_capacity,chunks):
    # Capacity reserves complete chunks; the live-object limit applies to count.
    return (0<=count<=2000000 and 0<=chunks<=chunk_capacity<=1024
            and capacity==chunk_capacity*65536 and count<=capacity
            and count<=chunks*65536)


def verify_name_prefix(names):
    expected=((0,'None'),(3,'ByteProperty'),(10,'IntProperty'),(17,'BoolProperty'),(24,'FloatProperty'))
    for entry_id,text in expected:
        if names.resolve(entry_id)!=text:
            raise NamePoolError('Prefixo da tabela de nomes incompatível.')
    return len(expected)


def inspect_metadata(process,roots):
    pool=process.base+roots['name_pool_rva']
    names=NamePoolReader(process.read,pool)
    prefix_count=verify_name_prefix(names)
    registry=process.base+roots['registry_rva']
    header=process.read(registry,48)
    chunks=struct.unpack_from('<Q',header,16)[0]
    maximum,count,max_chunks,num_chunks=struct.unpack_from('<iiii',header,32)
    if not valid_registry_limits(maximum,count,max_chunks,num_chunks):
        raise NamePoolError('Limites do registro de objetos inválidos.',details={
            'name_pool_sentinel_verified':True,'capacity':maximum,'count':count,
            'chunk_capacity':max_chunks,'chunks':num_chunks})
    classes=[];rejected=0
    if count:
        chunk=pointer(struct.unpack('<Q',process.read(pointer(chunks),8))[0])
        slots=process.read(chunk,min(count,32)*24)
        for index in range(min(count,32)):
            address=struct.unpack_from('<Q',slots,index*24)[0]
            if not address:continue
            try:
                object_header=process.read(pointer(address),40)
                if struct.unpack_from('<i',object_header,12)[0]!=index:
                    raise NamePoolError('Índice do objeto divergiu.')
                class_pointer=pointer(struct.unpack_from('<Q',object_header,16)[0])
                class_header=process.read(class_pointer,40)
                class_name_id=struct.unpack_from('<I',class_header,24)[0]
                class_name=names.resolve(class_name_id)
                if class_name not in classes:classes.append(class_name)
            except (OSError,NamePoolError):
                rejected+=1
    if process.read(registry,48)!=header:
        raise NamePoolError('Registro de objetos mudou durante a observação.')
    return {'name_pool_sentinel_verified':True,'object_count':count,
            'name_pool_prefix_entries_verified':prefix_count,
            'object_chunks':num_chunks,'sampled_class_names':classes,
            'rejected_sample_objects':rejected,'gameplay_validated':False}
