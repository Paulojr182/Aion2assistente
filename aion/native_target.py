"""Read target identity through reflected HUD links and validated native code.

Visible, unambiguous selection can be confirmed; hostility is a separate stage.
"""
import struct
from .native_player import observe_player,ObjectRegistry,PlayerObservationError
from .native_scene import observe_scene
from .reflection_fields import ReflectionSchema,ReflectionError
from .dialog_map import read_dialog_map
from .scene_objects import decode_key
from .target_code import widget_visibility,check_updater


def dialog_roots(registry,schema,game,name=None):
    ui=schema.linked(game,'AionGameInstance','UISystem')
    if ui is None:return []
    if ui['class_name']!='AionUISystem':raise ReflectionError('UISystem não validado.')
    field=schema.field(ui,'AionUISystem','LoadedDialogMap')
    if (field.kind,field.count,field.stride,field.type_id,field.cast_flags)!=('MapProperty',1,80,70368744177664,70368744210433):
        raise ReflectionError('Representação do mapa de diálogos mudou.')
    schema.default_accessors(field)
    extension=schema.read(field.address+112,48)
    key_prop,value_prop=struct.unpack_from('<QQ',extension)
    if struct.unpack_from('<6I',extension,16)!=(16,24,28,32,8,32):
        raise ReflectionError('Layout nativo de entradas de diálogo mudou.')
    for address,kind,stride,offset,type_id,cast in ((key_prop,'StructProperty',12,0,1048576,1081345),
                                                  (value_prop,'ObjectProperty',8,16,65536,67207169)):
        raw=schema.read(address,72);type_pointer,owner=struct.unpack_from('<QQ',raw,8)
        count,actual_stride=struct.unpack_from('<ii',raw,48);actual_offset=struct.unpack_from('<i',raw,68)[0]
        if owner!=field.address or (count,actual_stride,actual_offset)!=(1,stride,offset) or schema.type_info(type_pointer)!=(kind,type_id,cast):
            raise ReflectionError('Subtipo do mapa de diálogos mudou.')
    descriptor=struct.unpack('<Q',schema.read(key_prop+112,8))[0]
    if registry.object(descriptor)['class_name']!='ScriptStruct':raise ReflectionError('Chave de diálogo não é ScriptStruct.')
    raw=schema.read(descriptor,96);declaration_name,number=schema.name(raw[24:32])
    if (declaration_name,number,struct.unpack_from('<i',raw,88)[0])!=('DialogKey',0,12):raise ReflectionError('DialogKey mudou.')
    declared=struct.unpack('<Q',schema.read(value_prop+112,8))[0]
    if registry.class_info(declared)['name']!='AionDialog':raise ReflectionError('Tipo declarado do diálogo mudou.')
    roots=[]
    for key,address in read_dialog_map(registry.process.read,ui['address']+field.offset):
        if not address:continue
        if name is not None and schema.name(key[:8])!=(name,0):continue
        dialog=registry.object(address)
        if 'AionDialog' not in {item['name'] for item in registry.ancestry(dialog)}:
            raise ReflectionError('Entrada do mapa não pertence a AionDialog.')
        root=schema.linked(dialog,'AionDialog','RootWidget')
        if root and (name is not None or 'AionDialogWidget_HUD' in {item['name'] for item in registry.ancestry(root)}):roots.append(root)
    if len(roots)>1:raise ReflectionError('Raiz de diálogo ambígua.')
    return roots


def hud_roots(registry,schema,game):
    return dialog_roots(registry,schema,game)


def observe_target(process,roots,visibility_layout,definitions,resource_layout):
    player=observe_player(process,roots)
    report={'player_present':player['player_present'],'targets':[],'game_actions_executed':False,
            'selection_current_verified':False,'hostility_assessed':False}
    if not player['player_present']:report['stage']=player['stage'];return report
    registry=ObjectRegistry(process,roots);schema=ReflectionSchema(registry,max_bytes=1048576)
    registry.allow_widgets(player['roles']['player']['class_address'])
    scene=observe_scene(process,roots,known_keys_only=True);game=registry.object(player['roles']['game_instance']['address'])
    hud=hud_roots(registry,schema,game)
    if not hud:report['stage']='hud_root_absent';schema.verify();registry.verify();return report
    for role,name,expected in (('target','P_TargetInfo','HUD_TargetInfo_C'),('boss','P_TargetInfo_Boss','HUD_TargetInfoBoss_C')):
        widget=schema.linked(hud[0],'AionDialogWidget_HUD',name)
        if not widget:continue
        if widget['class_name']!=expected:raise ReflectionError('Classe concreta do alvo mudou.')
        visibility=widget_visibility(schema,widget,visibility_layout,definitions)
        updater=schema.linked(widget,'AionWidget_HUD_TargetInfo','HpUpdater')
        if updater is None:continue
        if updater['class_name']!='AionUIComponent_CharResUpdater' or updater['class_size']!=368:
            raise ReflectionError('Atualizador de recursos do alvo mudou.')
        if struct.unpack('<Q',schema.read(updater['address']+32,8))[0]!=widget['address']:
            raise ReflectionError('Atualizador pertence a outro widget.')
        selector=struct.unpack('<I',schema.read(updater['address']+64,4))[0]
        updater_validated=check_updater(schema,updater,definitions,resource_layout)
        source_flags_match=(schema.read(updater['address']+168,1)==b'\x01' and schema.read(updater['address']+104,1)==b'\x00')
        result={'role':role,'source_selector':selector,'selection_current_verified':False,
                'visibility':visibility,'native_updater_validated':updater_validated,'source_flags_match':source_flags_match}
        raw=schema.read(updater['address']+80,24)
        if raw[0] in (1,2) and struct.unpack_from('<Q',raw,8)[0]:
            key=decode_key(raw);matches={}
            for collection in scene['collections']:
                for entry in collection['entries']:
                    if (entry['key_tag'],int(entry['key_payload_decimal']),entry['key_extra'])==(key.tag,key.payload,key.extra):
                        matches[(entry['index'],entry['serial'])]=entry
            result.update(key_tag=key.tag,key_payload_decimal=str(key.payload),key_extra=key.extra,matching_scene_identities=len(matches))
            if len(matches)==1:
                entry=next(iter(matches.values()));result['actor']={key:entry[key] for key in ('index','serial','address','class_name')}
                result['selection_current_verified']=bool(visibility['locally_visible'] and updater_validated and selector==4 and source_flags_match)
        report['targets'].append(result)
    identities={(item['actor']['index'],item['actor']['serial']) for item in report['targets'] if item['selection_current_verified']}
    report['selection_current_verified']=len(identities)==1
    if len(identities)>1:
        report['selection_ambiguous']=True
        for item in report['targets']:item['selection_current_verified']=False
    schema.verify();registry.verify();report['stage']='hud_target_diagnostic_observed'
    return report
