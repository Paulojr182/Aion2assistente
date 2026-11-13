"""Find a matching interaction prompt through the current visible widget tree.

Source evidence: core.control_actions.find_widgets and ActionSnapshot.visible.
No UI input or native functions are invoked.
"""
from collections import deque
import struct
from .reflection_fields import ReflectionError
from .scene_objects import decode_key
from .target_code import widget_availability
from .widget_switcher_index import read_widget_switcher_index


def panel_children(registry,schema,panel,classes):
    field=schema.field(panel,'PanelWidget','Slots')
    if (field.kind,field.count,field.stride)!=('ArrayProperty',1,16):
        raise ReflectionError('Array de slots do painel possui outro tipo.')
    schema.default_accessors(field)
    storage,count,capacity=struct.unpack('<Qii',schema.read(panel['address']+field.offset,16))
    if not 0 <= count <= capacity <= 2500 or count and not storage:
        raise ReflectionError('Filhos do painel excedem o limite ou não possuem armazenamento.')
    indices=range(count)
    if 'WidgetSwitcher' in classes:
        active=schema.field(panel,'WidgetSwitcher','ActiveWidgetIndex')
        if (active.kind,active.count,active.stride)!=('IntProperty',1,4):
            raise ReflectionError('Índice ativo do WidgetSwitcher possui outro tipo.')
        selected=read_widget_switcher_index(schema,panel,active)
        if selected == -1:return ()
        if not 0 <= selected < count:raise ReflectionError('Página ativa fora do WidgetSwitcher.')
        indices=(selected,)
    children=[]
    for index in indices:
        address=struct.unpack('<Q',schema.read(storage+index*8,8))[0]
        slot=registry.object(address)
        if 'PanelSlot' not in {a['name'] for a in registry.ancestry(slot)}:
            raise ReflectionError('Filho do painel não é PanelSlot.')
        parent=schema.linked(slot,'PanelSlot','Parent')
        if parent is None or (parent['index'],parent['serial'])!=(panel['index'],panel['serial']):
            raise ReflectionError('Slot pertence a outro painel.')
        child=schema.linked(slot,'PanelSlot','Content')
        if child is not None:children.append(child)
    return tuple(children)


def walk_available_widgets(registry,schema,root,available,*,limit=4096):
    if type(limit) is not int or not 0 < limit <= 4096:
        raise ReflectionError('Limite de árvore de widgets inválido.')
    queue=deque(((root,None),))
    seen=set();objects={};parents={}
    while queue:
        widget,parent=queue.popleft()
        identity=widget['index'],widget['serial']
        if identity in seen or len(seen)>=limit:
            raise ReflectionError('Árvore de widgets cíclica, duplicada ou extensa.')
        seen.add(identity)
        classes={a['name'] for a in registry.ancestry(widget)}
        if 'Widget' not in classes:raise ReflectionError('A árvore contém um objeto sem hierarquia Widget.')
        if not available(widget):continue
        objects[identity]=widget;parents[identity]=parent
        if 'UserWidget' in classes:
            tree=schema.linked(widget,'UserWidget','WidgetTree')
            if tree is not None:
                if tree['class_name']!='WidgetTree':raise ReflectionError('Árvore do UserWidget não foi validada.')
                child=schema.linked(tree,'WidgetTree','RootWidget')
                if child is not None:queue.append((child,identity))
        if 'PanelWidget' in classes:
            for child in panel_children(registry,schema,widget,classes):queue.append((child,identity))
    return objects,parents


def matching_interaction_prompts(registry,schema,root,action,key,visibility,definitions):
    available=lambda widget:widget_availability(schema,widget,visibility,definitions)['available']
    objects,parents=walk_available_widgets(registry,schema,root,available)
    matches=[]
    for identity,widget in objects.items():
        if 'AionWidget_HUD_InteractionActionKey' not in {a['name'] for a in registry.ancestry(widget)}:continue
        if widget['class_size'] < 1000:raise ReflectionError('Widget de interação não comporta sua chave.')
        if schema.read(widget['address']+968,1)[0]!=action:continue
        current=decode_key(schema.read(widget['address']+976,24))
        if (current.tag,current.payload,current.extra)!=key:continue
        button=schema.linked(widget,'AionWidget_HUD_InteractionActionKey','P_Btn_Interaction')
        if button is None:continue
        if 'AionButton' not in {a['name'] for a in registry.ancestry(button)}:
            raise ReflectionError('Botão de interação possui outra classe.')
        button_id=button['index'],button['serial']
        current_id=button_id
        while current_id in parents and current_id!=identity:current_id=parents[current_id]
        if current_id!=identity:continue
        matches.append({'widget_identity':identity,'button_identity':button_id})
    if len(matches)>1:raise ReflectionError('Mais de um prompt disponível para a mesma interação.')
    return tuple(matches)
