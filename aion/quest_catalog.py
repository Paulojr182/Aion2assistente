"""Data-only decoding of reviewed QuestTableItem rows.

The caller must validate current code, vtable and reflected field contracts
before reading a row. This decoder grants no permission to execute a quest.
Contracts originate in retail_quest_definitions (quest_catalog_constants.json).
"""
from dataclasses import dataclass
import struct
from .quest_records import QuestRecordsError


QUEST_FIELDS={
    'ID':('StructProperty',8,None), 'Name':('NameProperty',12,None),
    'Race':('EnumProperty',20,'ERace'), 'UnlockLevel':('IntProperty',24,None),
    'RecommendedLevel':('IntProperty',28,None), 'QuestText':('NameProperty',32,None),
    'Type':('EnumProperty',40,'EQuestType'),
    'AcquireType':('EnumProperty',72,'EQuestAcquireType'),
    'AcquireValue':('NameProperty',76,None), 'AcquireMapId':('StructProperty',104,None),
    'CompleteType':('EnumProperty',168,'EQuestCompleteType'),
    'CompleteValue':('NameProperty',172,None), 'CompleteMapId':('StructProperty',184,None),
    'NextQuestName':('NameProperty',216,None)}


@dataclass(frozen=True)
class QuestDefinitionName:
    index:int
    number:int
    symbol:str


@dataclass(frozen=True)
class QuestDefinitionEnum:
    declaration:str
    raw:int
    symbol:str


@dataclass(frozen=True)
class QuestDefinition:
    quest_id:int
    name:QuestDefinitionName
    text_key:QuestDefinitionName
    race:QuestDefinitionEnum
    quest_type:QuestDefinitionEnum
    acquisition:QuestDefinitionEnum
    completion:QuestDefinitionEnum
    unlock_level:int
    recommended_level:int
    next_quest_name:QuestDefinitionName
    acquisition_target:QuestDefinitionName
    acquisition_map_id:int
    completion_target:QuestDefinitionName
    completion_map_id:int
    localized_title_observed:bool=False
    runtime_status_classified:bool=False


def decode_quest_definition(raw,expected_id,expected_vtable,resolve_name,enums):
    """Decode one bounded row with a verified current table join and enum map."""
    if (type(raw) is not bytes or len(raw)!=240 or type(expected_id) is not int
            or not 0<expected_id<=0xffffffff or type(expected_vtable) is not int
            or expected_vtable<=0 or struct.unpack_from('<Q',raw)[0]!=expected_vtable
            or struct.unpack_from('<I',raw,8)[0]!=expected_id):
        raise QuestRecordsError('Linha de definição não corresponde à identidade verificada.')

    def name(field):
        index,number=struct.unpack_from('<II',raw,QUEST_FIELDS[field][1])
        symbol=resolve_name(index)
        if not isinstance(symbol,str) or not symbol or '\0' in symbol:
            raise QuestRecordsError('Nome da definição de missão inválido.')
        return QuestDefinitionName(index,number,symbol)

    def enum(field):
        _,offset,declaration=QUEST_FIELDS[field]
        observed=enums.get(declaration)
        if (not isinstance(observed,dict) or observed.get('name')!=declaration
                or not isinstance(observed.get('entries'),(tuple,list))):
            raise QuestRecordsError('Declaração atual do enum da missão ausente.')
        values={};labels=set()
        for entry in observed['entries']:
            label=entry.get('name');decimal=entry.get('value_decimal')
            try:value=int(decimal)
            except (ValueError,TypeError) as exc:
                raise QuestRecordsError('Valor de enum da missão inválido.') from exc
            if (not isinstance(label,str) or not label.startswith(declaration+'::')
                    or label in labels or value in values):
                raise QuestRecordsError('Enum da missão ambíguo ou de outro tipo.')
            labels.add(label);values[value]=label
        value=raw[offset]
        if value not in values:raise QuestRecordsError('Valor da missão não consta no enum atual.')
        return QuestDefinitionEnum(declaration,value,values[value])

    def integer(field,fmt):return struct.unpack_from(fmt,raw,QUEST_FIELDS[field][1])[0]
    return QuestDefinition(expected_id,name('Name'),name('QuestText'),enum('Race'),enum('Type'),
        enum('AcquireType'),enum('CompleteType'),integer('UnlockLevel','<i'),
        integer('RecommendedLevel','<i'),name('NextQuestName'),name('AcquireValue'),
        integer('AcquireMapId','<I'),name('CompleteValue'),integer('CompleteMapId','<I'))
