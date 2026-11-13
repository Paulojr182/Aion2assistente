import json
import struct
from pathlib import Path

def probe_nav(reader):
    results = {}
    try:
        with reader.process(33554432) as process:
            from .native_player import observe_player, ObjectRegistry
            from .reflection_fields import ReflectionSchema

            player = observe_player(process, reader.roots)
            registry = ObjectRegistry(process, reader.roots)
            schema = ReflectionSchema(registry, max_bytes=2097152)

            pawn = registry.object(player['roles']['player']['address'])
            controller = schema.linked(pawn, 'Pawn', 'Controller')
            
            def get_all_fields_and_funcs(class_addr):
                fields = []
                funcs = []
                seen = set()
                curr = class_addr
                while curr and curr not in seen:
                    seen.add(curr)
                    info = registry.class_info(curr)
                    c_name = info['name']
                    # Read fields via schema.own_fields
                    try:
                        first_f = struct.unpack_from('<Q', info['raw'], 80)[0]
                        for f in schema.own_fields(curr, info['size'], first_f):
                            fields.append({'class': c_name, 'name': f.name, 'kind': f.kind, 'offset': f.offset})
                    except Exception:
                        pass
                    # Read children for UFunctions (offset 72 in UStruct)
                    try:
                        child = struct.unpack_from('<Q', info['raw'], 72)[0]
                        c_seen = set()
                        while child and child not in c_seen:
                            c_seen.add(child)
                            c_obj = registry.object(child)
                            if c_obj.get('class_name') == 'Function':
                                f_header = process.read(child, 40)
                                f_name_id = struct.unpack_from('<I', f_header, 24)[0]
                                f_name = registry.names.resolve(f_name_id)
                                funcs.append({'class': c_name, 'name': f_name, 'address': child})
                            child = struct.unpack_from('<Q', process.read(child + 32, 8))[0] if child else 0
                    except Exception:
                        pass
                    curr = info['parent']
                return fields, funcs

            pawn_fields, pawn_funcs = get_all_fields_and_funcs(pawn['class_address'])
            results['pawn_nav_fields'] = [f for f in pawn_fields if any(k in f['name'].lower() for k in ('move', 'nav', 'auto', 'path', 'dest', 'waypoint'))]
            results['pawn_nav_funcs'] = [f for f in pawn_funcs if any(k in f['name'].lower() for k in ('move', 'nav', 'auto', 'path', 'dest', 'waypoint'))]

            if controller:
                ctrl_fields, ctrl_funcs = get_all_fields_and_funcs(controller['class_address'])
                results['ctrl_nav_fields'] = [f for f in ctrl_fields if any(k in f['name'].lower() for k in ('move', 'nav', 'auto', 'path', 'dest', 'waypoint'))]
                results['ctrl_nav_funcs'] = [f for f in ctrl_funcs if any(k in f['name'].lower() for k in ('move', 'nav', 'auto', 'path', 'dest', 'waypoint'))]

            # Dialog check omitted
            results['probe_success'] = True

        log_file = Path(reader.base) / 'data' / 'nav_deep_probe.json'
        log_file.write_text(json.dumps(results, indent=2), encoding='utf-8')
    except Exception as e:
        import traceback
        err_file = Path(reader.base) / 'data' / 'nav_deep_probe.json'
        err_file.write_text(json.dumps({'error': str(e), 'trace': traceback.format_exc()}, indent=2), encoding='utf-8')
    return results
