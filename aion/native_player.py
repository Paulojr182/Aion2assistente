"""Read the original game-instance/session/manager/local-pawn chain.

Every selected object is checked against its current registry slot and class.
This module does not send input, write memory or call game functions.
"""
import struct
from .name_pool import NamePoolReader,NamePoolError,pointer,valid_registry_limits


class PlayerObservationError(ValueError):
    def __init__(self,message,*,details=None):
        super().__init__(message);self.details=details or {}


class RegistryChanged(PlayerObservationError):
    pass


class ObjectRegistry:
    def __init__(self,process,roots):
        self.process=process
        self.root=process.base+roots['registry_rva']
        self.names=NamePoolReader(process.read,process.base+roots['name_pool_rva'])
        if self.names.resolve(0)!='None':raise PlayerObservationError('Tabela de nomes não validada.')
        self.header=process.read(self.root,48)
        self.class_cache={}
        self.object_headers={}
        self.slot_journal={}
        self.widget_blueprint_anchor=None
        self.chunks=pointer(struct.unpack_from('<Q',self.header,16)[0])
        capacity,self.count,chunk_capacity,chunks=struct.unpack_from('<iiii',self.header,32)
        if not valid_registry_limits(capacity,self.count,chunk_capacity,chunks):
            raise PlayerObservationError('Limites do registro inválidos.')

    def slot(self,index):
        if type(index) is not int or not 0<=index<self.count:
            raise PlayerObservationError('Índice de objeto fora do registro.')
        chunk=pointer(struct.unpack('<Q',self.process.read(self.chunks+8*(index>>16),8))[0])
        slot_address=chunk+24*(index&65535)
        data=self.process.read(slot_address,24)
        if index in self.slot_journal and self.slot_journal[index]!=(slot_address,data):
            raise RegistryChanged('Slot mudou durante a observação.')
        self.slot_journal[index]=(slot_address,data)
        address=struct.unpack_from('<Q',data)[0]
        flags=struct.unpack_from('<I',data,8)[0]
        serial=struct.unpack_from('<I',data,16)[0]
        # SerialNumber is allocated for weak references; zero alone does not
        # invalidate a strong registry pointer (e.g. native class metadata).
        # Weak references are checked separately against their nonzero serial.
        # Epic: API/Runtime/CoreUObject/FUObjectArray/GetSerialNumber
        if not address or flags&0x30200000:
            raise PlayerObservationError('O objeto não está vivo no registro.',details={
                'object_index':index,'flags':hex(flags),'serial':serial,'pointer_present':bool(address)})
        return pointer(address),serial

    def object(self,address):
        pointer(address)
        data=self.process.read(address,40)
        index=struct.unpack_from('<i',data,12)[0]
        actual,serial=self.slot(index)
        if actual!=address:
            raise PlayerObservationError('A identidade do objeto mudou.',details={
                'object_index':index,'requested_address':address,'registry_address':actual})
        class_address=pointer(struct.unpack_from('<Q',data,16)[0])
        if address in self.object_headers and self.object_headers[address]!=data:
            raise RegistryChanged('Cabeçalho do objeto mudou durante a leitura.')
        self.object_headers[address]=data
        info=self.class_info(class_address)
        return {'address':address,'index':index,'serial':serial,'class_address':class_address,
                'class_name':info['name'],'class_size':info['size']}

    def class_info(self,class_address):
        if class_address in self.class_cache:return self.class_cache[class_address]
        class_data=self.process.read(class_address,96)
        class_index=struct.unpack_from('<i',class_data,12)[0]
        if self.slot(class_index)[0]!=class_address:
            raise PlayerObservationError('Classe ausente do registro atual.')
        meta_address=pointer(struct.unpack_from('<Q',class_data,16)[0])
        meta_header=self.process.read(meta_address,40)
        meta_name=self.names.resolve(struct.unpack_from('<I',meta_header,24)[0])
        if meta_name=='WidgetBlueprintGeneratedClass':
            if self.widget_blueprint_anchor is None:
                raise PlayerObservationError('Metaclasse de widget exige âncora do jogador.')
            widget_meta=self.class_info(meta_address)
            blueprint=self.class_info(self.widget_blueprint_anchor)
            class_meta=struct.unpack_from('<Q',meta_header,16)[0]
            if (widget_meta['parent']!=self.widget_blueprint_anchor or blueprint['name']!='BlueprintGeneratedClass'
                    or blueprint['parent']!=class_meta or self.class_info(class_meta)['name']!='Class'):
                raise PlayerObservationError('Ancestralidade exata da metaclasse de widget diverge da âncora do jogador.')
        elif meta_name not in ('Class','BlueprintGeneratedClass'):
            raise PlayerObservationError('Metaclasse não foi validada.',details={'metaclass_name':meta_name})
        class_name=self.names.resolve(struct.unpack_from('<I',class_data,24)[0])
        class_size=struct.unpack_from('<i',class_data,88)[0]
        if not 40<=class_size<=2097152:
            raise PlayerObservationError('Tamanho da classe não foi validado.')
        result={'name':class_name,'size':class_size,'raw':class_data,
                'parent':struct.unpack_from('<Q',class_data,64)[0]}
        self.class_cache[class_address]=result
        return result

    def allow_widgets(self,player_class_address):
        self.class_info(player_class_address)
        raw=self.class_cache[player_class_address]['raw']
        anchor=struct.unpack_from('<Q',raw,16)[0]
        if self.class_info(anchor)['name']!='BlueprintGeneratedClass':
            raise PlayerObservationError('Jogador não ancora BlueprintGeneratedClass.')
        self.widget_blueprint_anchor=anchor

    def resolve_pair(self,index,serial):
        if serial==0 or index<0:return None
        target,current_serial=self.slot(index)
        if serial!=current_serial:raise PlayerObservationError('Referência fraca expirou.')
        return self.object(target)

    def weak(self,address):
        before=self.process.read(pointer(address),8)
        index,serial=struct.unpack('<iI',before)
        if serial==0 or index<0:return None
        try:
            observed=self.resolve_pair(index,serial)
        except PlayerObservationError as exc:
            raise PlayerObservationError(str(exc),details={**exc.details,
                'weak_index':index,'expected_serial':serial}) from exc
        if self.process.read(address,8)!=before:
            raise PlayerObservationError('Referência fraca mudou durante a leitura.')
        return observed

    def linked(self,owner,offset,expected):
        if offset<40 or offset+8>owner['class_size']:
            raise PlayerObservationError('Campo fora da classe proprietária.')
        address=struct.unpack('<Q',self.process.read(owner['address']+offset,8))[0]
        if not address:return None
        observed=self.object(address)
        if observed['class_name']!=expected:
            raise PlayerObservationError('O campo não aponta para '+expected+'.')
        return observed

    def ancestry(self,object_):
        address=object_['class_address'];seen=set();chain=[]
        for _ in range(32):
            if address in seen:raise PlayerObservationError('Ciclo na hierarquia de classes.')
            seen.add(address);info=self.class_info(pointer(address))
            name=info['name'];size=info['size']
            if not 40<=size<=object_['class_size']:
                raise PlayerObservationError('Extensão da classe ancestral inválida.')
            chain.append({'name':name,'size':size})
            address=info['parent']
            if not address:return chain
        raise PlayerObservationError('Hierarquia excedeu o limite.')

    def verify(self):
        for address,data in self.slot_journal.values():
            if self.process.read(address,24)!=data:
                raise RegistryChanged('Slot mudou durante a verificação.')
        for address,data in self.object_headers.items():
            if self.process.read(address,40)!=data:
                raise RegistryChanged('Objeto mudou durante a verificação.')
        for address,info in self.class_cache.items():
            if self.process.read(address,96)!=info['raw']:
                raise RegistryChanged('Classe mudou durante a verificação.')
        if self.process.read(self.root,48)!=self.header:
            raise RegistryChanged('Registro mudou durante a observação.')


def observe_player(process,roots):
    def checked(stage,function,*args):
        try:return function(*args)
        except PlayerObservationError as exc:
            raise PlayerObservationError(str(exc),details={**exc.details,'stage':stage}) from exc
    registry=ObjectRegistry(process,roots)
    report={'player_present':False,'roles':{},'game_actions_executed':False,'gameplay_validated':False}
    game=checked('global_game_instance',registry.weak,process.base+roots['global_weak_rva'])
    if not game:report['stage']='game_instance_absent';return report
    if game['class_name']!='AionGameInstance':
        raise PlayerObservationError('A referência global não é AionGameInstance.')
    report['roles']['game_instance']=game
    session=checked('character_session',registry.linked,game,1072,'AionCharSession')
    if not session:report['stage']='session_absent';return report
    report['roles']['session']=session
    manager=checked('object_manager',registry.linked,session,56,'AionObjectManager')
    if not manager:report['stage']='object_manager_absent';return report
    report['roles']['object_manager']=manager
    if manager['class_size']<80:raise PlayerObservationError('Gerenciador sem a referência local esperada.')
    player=checked('local_player',registry.weak,manager['address']+72)
    if not player:report['stage']='player_absent';return report
    chain=registry.ancestry(player)
    if 'AionCharacter' not in {item['name'] for item in chain}:
        raise PlayerObservationError('O jogador não possui a hierarquia AionCharacter.')
    report['roles']['player']=player;report['player_ancestry']=chain
    for object_ in report['roles'].values():
        actual=registry.object(object_['address'])
        if (actual['index'],actual['serial'],actual['class_name'])!=(object_['index'],object_['serial'],object_['class_name']):
            raise PlayerObservationError('A identidade da cadeia mudou durante a verificação.')
    registry.verify();report['player_present']=True;report['stage']='player_metadata_observed'
    return report
