"""Bounded reflected-property reader matching the original metadata layout."""
from dataclasses import dataclass
import struct
from .name_pool import pointer
from .native_player import PlayerObservationError


class ReflectionError(ValueError):
    def __init__(self,message,*,details=None):
        super().__init__(message);self.details=details or {}


class ReflectionChanged(ReflectionError):
    pass


@dataclass(frozen=True)
class Property:
    address:int
    owner:int
    name:str
    number:int
    kind:str
    offset:int
    count:int
    stride:int
    vtable:int
    type_id:int
    cast_flags:int


class ReflectionSchema:
    def __init__(self,registry,*,max_bytes=131072,max_fields=512):
        self.registry=registry;self.process=registry.process;self.names=registry.names
        self.limit=max_bytes;self.used=0;self.max_fields=max_fields
        self.journal={};self.field_cache={};self.type_cache={};self.accessors={};self.code_cache={}
        if not 1<=max_bytes<=2097152 or not 1<=max_fields<=512:
            raise ReflectionError('Limites de reflexão inválidos.')

    def read(self,address,size):
        pointer(address)
        if not 0<size<=self.limit-self.used:raise ReflectionError('Orçamento de metadados esgotado.')
        self.used+=size;raw=self.process.read(address,size)
        if type(raw) is not bytes or len(raw)!=size:raise ReflectionError('Leitura de metadados incompleta.')
        key=(address,size)
        if key in self.journal and self.journal[key]!=raw:
            raise ReflectionChanged('Metadados mudaram durante a leitura.',details={
                'address':address,'size':size,'before':self.journal[key][:64].hex(),'after':raw[:64].hex()})
        self.journal[key]=raw;return raw

    def name(self,raw):
        index,number=struct.unpack('<II',raw)
        return self.names.resolve(index),number

    def read_code(self,address,size):
        if (type(address) is not int or type(size) is not int or size<=0
                or not self.process.base<=address<=self.process.base+self.process.image_size-size):
            raise ReflectionError('Leitura de código fora da imagem.')
        key=address,size
        if key not in self.code_cache:self.code_cache[key]=self.read(address,size)
        # read() journals the initial bytes; verify() rechecks every code range
        # before publishing. This cache lives only for this observation/schema.
        return self.code_cache[key]

    def type_info(self,address):
        if address not in self.type_cache:
            raw=self.read(pointer(address),56);name,number=self.name(raw[:8])
            if number:raise ReflectionError('Tipo de propriedade possui sufixo inesperado.')
            type_id,cast_flags,_=struct.unpack_from('<QQI',raw,8)
            self.type_cache[address]=(name,type_id,cast_flags)
        return self.type_cache[address]

    def own_fields(self,owner,size,first_field):
        if owner in self.field_cache:return self.field_cache[owner]
        result=[];seen=set();address=first_field
        while address:
            if address in seen:raise ReflectionError('Ciclo na lista de propriedades.')
            if len(result)>=self.max_fields:raise ReflectionError('Limite de propriedades excedido.')
            seen.add(address);raw=self.read(pointer(address),72)
            type_pointer,tagged_owner,following=struct.unpack_from('<QQQ',raw,8)
            if tagged_owner!=owner|1:raise ReflectionError('Propriedade pertence a outro proprietário.')
            name,number=self.name(raw[32:40])
            count,stride=struct.unpack_from('<ii',raw,48);offset=struct.unpack_from('<i',raw,68)[0]
            if count<=0 or stride<=0 or offset<0 or count*stride>size-offset:
                raise ReflectionError('Extensão da propriedade excede sua classe.',details={
                    'property_name':name,'count':count,'stride':stride,'offset':offset,'owner_size':size})
            kind,type_id,cast_flags=self.type_info(type_pointer)
            result.append(Property(address,owner,name,number,kind,offset,count,stride,
                                   pointer(struct.unpack_from('<Q',raw)[0]),type_id,cast_flags))
            address=following
        self.field_cache[owner]=tuple(result)
        return self.field_cache[owner]

    def field(self,object_,owner_name,field_name):
        address=object_['class_address'];seen=set();owners=[]
        while address:
            if address in seen or len(seen)>=32:raise ReflectionError('Hierarquia de propriedade inválida.')
            seen.add(address);info=self.registry.class_info(address)
            if info['name']==owner_name:owners.append((address,info))
            address=info['parent']
        if len(owners)!=1:raise ReflectionError('Classe proprietária ausente ou ambígua: '+owner_name)
        address,info=owners[0]
        matches=[p for p in self.own_fields(address,info['size'],struct.unpack_from('<Q',info['raw'],80)[0])
                 if p.name==field_name and p.number==0]
        if len(matches)!=1:raise ReflectionError('Propriedade ausente ou ambígua: '+field_name)
        return matches[0]

    def default_accessors(self,field):
        cache_key=(field.vtable,field.address)
        if cache_key not in self.accessors:
            getters=struct.unpack('<QQ',self.read(field.vtable+120,16))
            for function in getters:
                if not self.process.base<=function<=self.process.base+self.process.image_size-3:
                    raise ReflectionError('Acessor de propriedade fora da imagem.')
                prefix=self.read(function,3)
                if prefix not in (b'\x32\xc0\xc3',b'\x33\xc0\xc3'):
                    # Reviewed HasGetter/HasSetter implementation: compare a
                    # property-owned pointer against zero, setne AL, return.
                    # A zero pointer proves this specific property has no
                    # custom accessor; nonzero remains a hard refusal.
                    code=self.read(function,9)
                    member=112 if code==b'\x48\x83\x79\x70\x00\x0f\x95\xc0\xc3' else (
                        120 if code==b'\x48\x83\x79\x78\x00\x0f\x95\xc0\xc3' else None)
                    if member is not None and self.read(field.address+member,8)==bytes(8):continue
                    detail={
                        'property':field.name,'kind':field.kind,'property_offset':field.offset,
                        'conditional_member_offset':member,
                        'conditional_member_value':self.read(field.address+member,8).hex() if member is not None else None,
                        'accessor_rva':function-self.process.base,'accessor_bytes':self.read(function,16).hex()}
                    if member is not None:
                        target=struct.unpack('<Q',self.read(field.address+member,8))[0]
                        if self.process.base<=target<=self.process.base+self.process.image_size-64:
                            detail.update(conditional_target_rva=target-self.process.base,
                                conditional_target_bytes=self.read(target,64).hex())
                        slots=struct.unpack('<QQ',self.read(field.address+112,16))
                        detail['property_accessor_slots']=[]
                        for slot in slots:
                            entry={'pointer':slot}
                            if self.process.base<=slot<=self.process.base+self.process.image_size-32:
                                entry.update(rva=slot-self.process.base,code=self.read(slot,32).hex())
                            detail['property_accessor_slots'].append(entry)
                        detail['query_methods']=[{'rva':q-self.process.base,'code':self.read(q,9).hex()} for q in getters]
                    raise ReflectionError('Propriedade usa acessor não validado.',details=detail)
            self.accessors[cache_key]=getters

    def linked(self,object_,owner,name,expected_offset=None):
        field=self.field(object_,owner,name)
        if (field.kind!='ObjectProperty' or field.count!=1 or field.stride!=8
                or expected_offset is not None and field.offset!=expected_offset):
            raise ReflectionError('Representação do vínculo não foi validada: '+name,details={
                'kind':field.kind,'count':field.count,'stride':field.stride,'offset':field.offset})
        self.default_accessors(field)
        address=struct.unpack('<Q',self.read(object_['address']+field.offset,8))[0]
        if not address:return None
        try:return self.registry.object(address)
        except PlayerObservationError as exc:
            raise PlayerObservationError(str(exc),details={**exc.details,
                'property_owner':owner,'property_name':name,'property_offset':field.offset}) from exc

    def vector_metadata(self,component):
        field=self.field(component,'SceneComponent','RelativeLocation')
        if field.kind!='StructProperty' or field.offset!=328 or field.count!=1 or field.stride!=24:
            raise ReflectionError('Representação do vetor não foi validada.')
        self.default_accessors(field)
        vector=struct.unpack('<Q',self.read(field.address+112,8))[0]
        if self.registry.object(pointer(vector))['class_name']!='ScriptStruct':
            raise ReflectionError('O vetor não possui metadados ScriptStruct.')
        raw=self.read(vector,96);name,number=self.name(raw[24:32])
        size,alignment=struct.unpack_from('<ii',raw,88)
        if (name,number,size,alignment)!=('Vector',0,24,8):
            raise ReflectionError('Tipo ou alinhamento do vetor não corresponde ao esperado.')
        fields=self.own_fields(vector,24,struct.unpack_from('<Q',raw,80)[0])
        for name,offset in (('X',0),('Y',8),('Z',16)):
            matches=[p for p in fields if p.name==name and p.number==0]
            if len(matches)!=1:raise ReflectionError('Componente de vetor ausente ou ambíguo.')
            p=matches[0]
            if (p.kind,p.offset,p.count,p.stride)!=('DoubleProperty',offset,1,8):
                raise ReflectionError('Componente de vetor não usa double64 esperado.')
            self.default_accessors(p)
        return field

    def enum_values(self,field):
        if (field.kind,field.count,field.stride)!=('EnumProperty',1,1):
            raise ReflectionError('Enum não possui representação byte validada.')
        self.default_accessors(field)
        underlying,enum=struct.unpack('<QQ',self.read(field.address+112,16))
        pointer(underlying);pointer(enum)
        object_=self.registry.object(enum)
        if (object_['class_name'],object_['class_size'])!=('Enum',104):
            raise ReflectionError('Declaração não pertence à classe Enum validada.')
        header=self.read(enum,80);name,number=self.name(header[24:32])
        if number:raise ReflectionError('Declaração de enum possui sufixo inesperado.')
        storage,count,capacity=struct.unpack_from('<Qii',header,64)
        if not 0<count<=512 or not count<=capacity<=8192 or storage%8:
            raise ReflectionError('Array de enum fora dos limites.')
        raw=self.read(pointer(storage),count*16);entries=[];labels=set()
        for index in range(count):
            ident,number,value=struct.unpack_from('<IIq',raw,index*16)
            if number:raise ReflectionError('Nome de valor de enum possui sufixo inesperado.')
            label=self.names.resolve(ident)
            if label in labels:raise ReflectionError('Nome de valor de enum duplicado.')
            labels.add(label);entries.append({'name':label,'value_decimal':str(value)})
        return {'name':name,'entries':entries}

    def verify(self):
        for (address,size),expected in self.journal.items():
            if size>self.limit-self.used:raise ReflectionError('Orçamento de verificação esgotado.')
            self.used+=size
            raw=self.process.read(address,size)
            if raw!=expected:raise ReflectionChanged('Metadados mudaram na verificação.',details={
                'address':address,'size':size,'before':expected[:64].hex(),'after':raw[:64].hex()})
