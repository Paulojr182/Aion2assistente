"""Bounded read-only candidates for the consumer of the captured forward-axis field."""
import struct
from collections import deque

def axis_memory_references(code,rva):
    # Analysis dependency is loaded by the diagnostic entrypoint.
    import capstone
    decoder=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_64);decoder.detail=True
    hits=[]
    for ins in decoder.disasm(code,rva):
        if any(op.type==capstone.CS_OP_MEM and op.mem.disp==0x9a8 and op.size in (8,16,32) for op in ins.operands):
            hits.append({'rva':ins.address,'instruction':ins.mnemonic+' '+ins.op_str})
    return hits

def direct_input_calls(code,rva):
    import capstone
    decoder=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_64);decoder.detail=True
    targets=[]
    for ins in decoder.disasm(code,rva):
        if ins.mnemonic=='ret':break
        if ins.mnemonic in ('call','jmp') and len(ins.operands)==1 and ins.operands[0].type==capstone.CS_OP_IMM:
            targets.append(ins.operands[0].imm)
        if ins.mnemonic=='jmp':break
    return sorted(set(targets))


def capture_controller_axis_consumers(process,roots,bindings):
    from .native_player import ObjectRegistry
    registry=ObjectRegistry(process,roots)
    ref=bindings['frame_objects']['controller']
    current=registry.object(ref['address'])
    if any(current[key]!=ref[key] for key in ('address','index','serial')):
        raise ValueError('Controlador mudou antes da leitura.')
    address=ref['address'];vtable=struct.unpack('<Q',process.read(address,8))[0]
    if not process.base<=vtable<=process.base+process.image_size-3072:
        raise ValueError('Tabela candidata fora da imagem.')
    table=process.read(vtable,3072)
    result={'read_only':True,'game_functions_called':False,'dispatch_approved':False,
            'scope':'384 pointer roots, direct-call depth 2, at most 1536 blocks of 4096 bytes; candidates only',
            'controller':{key:current[key] for key in ('address','index','serial','class_name')},
            'vtable_rva':vtable-process.base,'candidates':[],'unreadable':[],'scanned':0,'blocks':[]}
    pending=deque()
    for offset in range(0,len(table),8):
        target=struct.unpack_from('<Q',table,offset)[0]
        pending.append((target,0,offset))
    seen=set()
    while pending and len(seen)<1536:
        target,depth,offset=pending.popleft()
        if target in seen or not process.base<=target<=process.base+process.image_size-4096:continue
        seen.add(target)
        try:
            code=process.read(target,4096);result['scanned']+=1
        except OSError as exc:
            result['unreadable'].append({'rva':target-process.base,'error':str(exc)});continue
        location=target-process.base
        calls=direct_input_calls(code,location)
        result['blocks'].append({'rva':location,'depth':depth,'pointer_offset':offset,
                                 'code_hex':code.hex(),'direct_calls':calls})
        hits=axis_memory_references(code,location)
        if hits:
            result['candidates'].append({'pointer_offset':offset,'rva':location,'depth':depth,
                                         'references':hits})
        if depth<2:
            for call in calls:pending.append((process.base+call,depth+1,None))
    result['limit_reached']=bool(pending)
    if process.read(address,8)!=struct.pack('<Q',vtable) or process.read(vtable,3072)!=table:
        raise ValueError('Tabela mudou durante a leitura.')
    after=registry.object(address)
    if any(after[key]!=current[key] for key in ('index','serial','class_name')):
        raise ValueError('Controlador mudou durante a leitura.')
    registry.verify()
    return result
