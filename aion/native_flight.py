"""Read-only discovery of flight-related game functions; never dispatches calls."""
import hashlib
import struct
from .native_player import ObjectRegistry,observe_player,PlayerObservationError
from .flight_analysis import inspect_flight_entry
from .reflection_fields import ReflectionSchema


def read_movement_component(schema,pawn):
    component=schema.linked(pawn,'Character','CharacterMovement',968)
    if component is None or component['class_name']!='AionCharMovementComponent':
        raise ValueError('Componente de movimento atual não validado.')
    movement={'address':component['address'],'identity':[component['index'],component['serial']],'class':component['class_name']}
    for name,offset in (('MovementMode',545),('CustomMovementMode',546)):
        field=schema.field(component,'CharacterMovementComponent',name)
        if (field.kind,field.offset,field.count,field.stride,field.number)!=('ByteProperty',offset,1,1,0):
            raise ValueError('Representação refletida do modo de movimento mudou.')
        schema.default_accessors(field)
        movement[name]=schema.read(component['address']+offset,1)[0]
    return component,movement


def discover(process,roots,*,max_slots=65536,max_functions=64,include_movement=False):
    if (type(max_slots) is not int or not 1<=max_slots<=65536
            or type(max_functions) is not int or not 1<=max_functions<=128):
        raise ValueError('Limites de descoberta inválidos.')
    player=observe_player(process,roots)
    if not player['player_present']:raise ValueError('Personagem ausente.')
    registry=ObjectRegistry(process,roots)
    pawn=registry.object(player['roles']['player']['address'])
    owners={}
    def hierarchy(address):
        seen=set()
        while address:
            if address in seen or len(seen)>=32:raise ValueError('Hierarquia inválida.')
            seen.add(address)
            info=registry.class_info(address);owners[address]=info['name'];address=info['parent']
    hierarchy(pawn['class_address'])
    schema=None;movement=None
    if include_movement:
        schema=ReflectionSchema(registry,max_bytes=1048576)
        component,movement=read_movement_component(schema,pawn)
        hierarchy(component['class_address'])
    rows=[];journal=[];unavailable=0
    limit=min(registry.count,max_slots)
    for index in range(limit):
        try:address,_=registry.slot(index)
        except PlayerObservationError:
            unavailable+=1;continue
        header=process.read(address,40)
        owner=struct.unpack_from('<Q',header,32)[0]
        if owner not in owners:continue
        ident,number=struct.unpack_from('<II',header,24)
        name=registry.names.resolve(ident)
        if number or not any(word in name.lower() for word in ('fly','flight','glide','wing','movementmode')):continue
        obj=registry.object(address)
        if obj['class_name']!='Function':continue
        if len(rows)>=max_functions:raise ValueError('Limite de funções excedido; captura não publicada.')
        if obj['class_size']<224:raise ValueError('Função com extensão inesperada.')
        native=process.read(address+216,8);entry=struct.unpack('<Q',native)[0]
        rva=entry-process.base
        if not 0<=rva<=process.image_size-512:raise ValueError('Thunk fora da imagem.')
        code=process.read(entry,512);journal.extend(((address+216,native),(entry,code)))
        rows.append({'name':name,'owner':owners[owner],
                     'function_identity':[obj['index'],obj['serial']],
                     'code_sha256':hashlib.sha256(code).hexdigest(),
                     'code':code.hex(),'analysis':inspect_flight_entry(code,rva,process.image_size)})
    for address,expected in journal:
        if process.read(address,len(expected))!=expected:raise ValueError('Código/função mudou durante a captura.')
    if schema is not None:schema.verify()
    registry.verify()
    return {'session':process.session,'player_identity':[pawn['index'],pawn['serial']],
        'slots_examined':limit,'registry_slots':registry.count,'unavailable_slots':unavailable,
        'registry_scan_complete':limit==registry.count,'functions':rows,'movement':movement,
        'flight_transition_verified':False,'game_actions_executed':False,'approved_in_game':False}



def observe_flight_state(process,roots,*,with_position=False):
    player=observe_player(process,roots)
    if not player['player_present']:raise ValueError('Player absent')
    registry=ObjectRegistry(process,roots)
    pawn=registry.object(player['roles']['player']['address'])
    schema=ReflectionSchema(registry,max_bytes=1048576)
    _,movement=read_movement_component(schema,pawn)
    position=None
    if with_position:
        from .native_positions import actor_position
        position=actor_position(registry,schema,pawn)
    schema.verify();registry.verify()
    current=observe_player(process,roots)
    if not current['player_present'] or any(current['roles']['player'][k]!=pawn[k] for k in ('address','index','serial')):
        raise ValueError('Player changed during flight observation')
    return {'session':process.session,'player_identity':[pawn['index'],pawn['serial']],'movement':movement,
            'position':position}
