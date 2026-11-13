"""Interpret a reviewed read-only WidgetSwitcher getter without invoking it.

Game-only evidence: read_gather_accessor_bodies.json and
read_gather_switcher_attribute.json. Bound Slate attributes remain unsupported.
"""
import struct
from .metadata_templates import InstructionTemplate
from .name_pool import pointer
from .reflection_fields import ReflectionError

GETTER_BYTES=bytes.fromhex('40534883ec20488b81a8010000488bda4885c07410488bc8e8c38a5dfe89034883c4205bc38b81a001000089024883c4205bc3')
ATTRIBUTE_BYTES=bytes.fromhex('40534883ec2083b90802000000488bd97442488b89000200004885c9742a488b01ff503084c0742083bb08020000007409488b8b00020000eb0233c9488b01ff50588983f80100008b83f80100004883c4205bc38b81f80100004883c4205bc3')
HAS_GETTER_BYTES=bytes.fromhex('48837978000f95c0c3')


def read_widget_switcher_index(schema,panel,field):
    if (field.kind,field.count,field.stride,field.number)!=('IntProperty',1,4,0):
        raise ReflectionError('Índice do WidgetSwitcher possui outro contrato.')
    try:
        schema.default_accessors(field)
    except ReflectionError:
        if field.offset!=416:raise ReflectionError('Getter personalizado possui outro contrato de campo.')
        process=schema.process
        query=struct.unpack('<Q',schema.read(field.vtable+128,8))[0]
        if (not process.base<=query<=process.base+process.image_size-len(HAS_GETTER_BYTES)
                or schema.read(query,len(HAS_GETTER_BYTES))!=HAS_GETTER_BYTES):
            raise ReflectionError('Consulta do getter de WidgetSwitcher mudou.')
        getter=struct.unpack('<Q',schema.read(field.address+120,8))[0]
        rva=getter-process.base
        if not 0<=rva<=process.image_size-len(GETTER_BYTES):
            raise ReflectionError('Getter do WidgetSwitcher fora da imagem.')
        raw=schema.read(getter,len(GETTER_BYTES))
        template=InstructionTemplate('WidgetSwitcher.ActiveWidgetIndex getter',0,GETTER_BYTES,((25,29),))
        helper_rva,=template.targets(raw,rva,process.image_size)
        if (helper_rva>process.image_size-len(ATTRIBUTE_BYTES)
                or schema.read(process.base+helper_rva,len(ATTRIBUTE_BYTES))!=ATTRIBUTE_BYTES):
            raise ReflectionError('Getter do atributo Slate mudou.')
        native=struct.unpack('<Q',schema.read(panel['address']+424,8))[0]
        if native:
            pointer(native)
            raw=schema.read(native+504,20)
            if struct.unpack_from('<I',raw,16)[0]!=0:
                raise ReflectionError('Índice Slate usa binding dinâmico; leitura não autorizada.')
            return struct.unpack_from('<i',raw)[0]
    return struct.unpack('<i',schema.read(panel['address']+field.offset,4))[0]
