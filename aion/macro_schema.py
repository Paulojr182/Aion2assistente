"""Bounded HUD metadata discovery for the game's native macro action."""
import re,struct
from .native_player import observe_player,ObjectRegistry,PlayerObservationError
from .reflection_fields import ReflectionSchema,ReflectionError
from .native_target import hud_roots
from .combat_schema import method_inventory

LINK=re.compile('macro|autobattle|quickslot|skill',re.I)
STATE=re.compile('macro|autobattle|running|playing|active|checked|state',re.I)


def scalar_accessible(schema,field,entry):
    try:schema.default_accessors(field)
    except ReflectionError as exc:
        entry['value_unavailable_reason']=str(exc);return False
    return True


def observe_macro_schema(process,roots):
    player=observe_player(process,roots)
    result={'player_present':player['player_present'],'objects':[],'game_actions_executed':False,'macro_state_validated':False}
    if not player['player_present']:result['stage']=player['stage'];return result
    registry=ObjectRegistry(process,roots);registry.allow_widgets(player['roles']['player']['class_address'])
    schema=ReflectionSchema(registry,max_bytes=2097152)
    game=registry.object(player['roles']['game_instance']['address']);hud=hud_roots(registry,schema,game)
    if not hud:result['stage']='hud_root_absent';schema.verify();registry.verify();return result
    queue=[('HUD',hud[0],0)];seen=set();truncated=False
    while queue and len(result['objects'])<24:
        queue.sort(key=lambda item:('autobattle' not in item[0].lower(),'macro' not in item[0].lower(),item[2]))
        label,object_,depth=queue.pop(0);focused='autobattle' in label.lower()
        identity=(object_['index'],object_['serial'])
        if identity in seen:continue
        seen.add(identity);address=object_['class_address'];owners=set();report={'path':label,'class_name':object_['class_name'],'class_size':object_['class_size'],'matches':[],'methods':[]}
        while address:
            if address in owners or len(owners)>=32:raise ReflectionError('Hierarquia de HUD inválida.')
            owners.add(address);info=registry.class_info(address)
            # Generic engine widget state is not evidence of macro execution.
            if info['name'] in ('UserWidget','Widget','Object'):break
            fields=schema.own_fields(address,info['size'],struct.unpack_from('<Q',info['raw'],80)[0])
            for field in fields:
                if not (focused or LINK.search(field.name) or depth>0 and STATE.search(field.name)):continue
                entry={'owner':info['name'],'name':field.name,'kind':field.kind,'offset':field.offset,'count':field.count,'stride':field.stride}
                if field.kind=='ObjectProperty' and field.count==1 and field.stride==8:
                    try:
                        linked=schema.linked(object_,info['name'],field.name)
                        if linked:
                            entry['linked_class']=linked['class_name']
                            if depth<2:queue.append((label+'.'+field.name,linked,depth+1))
                        else:entry['linked_absent']=True
                    except (PlayerObservationError,ReflectionError) as exc:entry['unavailable_reason']=str(exc)
                elif field.kind=='EnumProperty' and field.count==1 and field.stride==1:
                    if scalar_accessible(schema,field,entry):
                        try:entry['enum']=schema.enum_values(field)
                        except ReflectionError as exc:entry['value_unavailable_reason']=str(exc)
                        else:entry['raw_value']=schema.read(object_['address']+field.offset,1)[0]
                elif field.kind in ('BoolProperty','ByteProperty') and field.count==1 and field.stride==1:
                    if scalar_accessible(schema,field,entry):
                        entry['raw_byte']=schema.read(object_['address']+field.offset,1)[0]
                        if field.kind=='BoolProperty':entry['bool_descriptor_hex']=schema.read(field.address+112,4).hex()
                elif field.kind in ('IntProperty','UInt32Property') and field.count==1 and field.stride==4:
                    if scalar_accessible(schema,field,entry):
                        entry['value']=struct.unpack('<i' if field.kind=='IntProperty' else '<I',schema.read(object_['address']+field.offset,4))[0]
                report['matches'].append(entry)
            try:methods=method_inventory(registry,schema,address,info)
            except ReflectionError as exc:report.setdefault('method_inventory_unavailable',[]).append({'owner':info['name'],'reason':str(exc)})
            else:report['methods'].extend({'owner':info['name'],**method} for method in methods if STATE.search(method['name']))
            address=info['parent']
        result['objects'].append(report)
    if queue:truncated=True
    schema.verify();registry.verify();result.update(stage='macro_hud_metadata_observed',report_truncated=truncated)
    return result
