"""Bounded static inspection of game function bytes; no execution or writes."""
from pathlib import Path
import sys


def inspect_flight_entry(raw,rva,image_size):
    if (type(raw) is not bytes or not 0<len(raw)<=4096 or type(rva) is not int
            or type(image_size) is not int or not 0<=rva<=image_size-len(raw)):
        raise ValueError('Limites de código inválidos.')
    dependency=str(Path(__file__).resolve().parents[1]/'analysis_deps')
    if dependency not in sys.path:sys.path.insert(0,dependency)
    from capstone import Cs,CS_ARCH_X86,CS_MODE_64,CS_AC_WRITE
    from capstone.x86 import X86_OP_MEM,X86_OP_IMM,X86_REG_RIP,X86_REG_INVALID
    decoder=Cs(CS_ARCH_X86,CS_MODE_64);decoder.detail=True
    calls=[];writes=[];instructions=[];stop='capture_end_or_undecodable'
    for ins in decoder.disasm(raw,rva):
        instructions.append({'rva':ins.address,'bytes':ins.bytes.hex(),
                             'mnemonic':ins.mnemonic,'operands':ins.op_str})
        for operand in ins.operands:
            if (operand.type==X86_OP_MEM and operand.size==1 and operand.access&CS_AC_WRITE
                    and operand.mem.disp==545 and operand.mem.base not in (X86_REG_RIP,X86_REG_INVALID)):
                immediate=ins.operands[-1].imm if ins.operands[-1].type==X86_OP_IMM else None
                writes.append({'rva':ins.address,'base_register':ins.reg_name(operand.mem.base),
                    'immediate':immediate,'candidate_mode_5':immediate==5,
                    'component_owner_proven':False})
        if ins.mnemonic=='call' and ins.operands and ins.operands[0].type==X86_OP_IMM:
            target=ins.operands[0].imm
            calls.append({'rva':ins.address,'target_rva':target,'target_in_image':0<=target<image_size})
        if ins.mnemonic in ('ret','retf','int3','jmp'):
            stop=ins.mnemonic;break
    return {'rva':rva,'entry_instructions':instructions,'direct_calls':calls,
            'movement_mode_write_candidates':writes,'entry_scan_stopped_at':stop,
            'full_control_flow_analyzed':False,'flight_transition_verified':False,
            'game_actions_executed':False}
