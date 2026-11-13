"""Derive table roots and constructor vtables from current accessor instructions.

Uses the project's existing Capstone analysis dependency, only in this process.
The caller must independently verify the code that calls this accessor.
"""
from pathlib import Path
import sys
from .native_u32_table import TableReadError


def decode_table_accessor(raw, rva, image_size):
    if (type(raw) is not bytes or not raw or len(raw) > 4096
            or type(rva) is not int or not 0 <= rva <= image_size-len(raw)):
        raise TableReadError('Limites do acessor de tabela inválidos.')
    try:
        from capstone import Cs, CS_ARCH_X86, CS_MODE_64
        from capstone.x86 import X86_OP_MEM, X86_OP_REG, X86_REG_RIP, X86_REG_RBX, X86_REG_RAX
    except ImportError:
        dependency = str(Path(__file__).resolve().parents[1]/'analysis_deps')
        if dependency not in sys.path:sys.path.insert(0, dependency)
        from capstone import Cs, CS_ARCH_X86, CS_MODE_64
        from capstone.x86 import X86_OP_MEM, X86_OP_REG, X86_REG_RIP, X86_REG_RBX, X86_REG_RAX
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    instructions = list(md.disasm(raw, rva))
    roots = []
    for ins in instructions:
        ops = ins.operands
        if (ins.mnemonic == 'lea' and len(ops) == 2 and ops[0].type == X86_OP_REG
                and ops[0].reg == X86_REG_RBX and ops[1].type == X86_OP_MEM
                and ops[1].mem.base == X86_REG_RIP):
            roots.append(ins.address+ins.size+ops[1].mem.disp)
    if len(roots) != 1 or not 0 <= roots[0] <= image_size-188:
        raise TableReadError('Raiz de tabela ausente, ambígua ou fora da imagem.')
    root = roots[0]
    rax = None
    vtables = []
    for ins in instructions:
        ops = ins.operands
        if (ins.mnemonic == 'lea' and len(ops) == 2 and ops[0].type == X86_OP_REG
                and ops[0].reg == X86_REG_RAX):
            rax = (ins.address+ins.size+ops[1].mem.disp
                   if ops[1].type == X86_OP_MEM and ops[1].mem.base == X86_REG_RIP else None)
        elif (ins.mnemonic == 'mov' and len(ops) == 2 and ops[0].type == X86_OP_MEM
                and ops[0].mem.base == X86_REG_RIP and ops[1].type == X86_OP_REG
                and ops[1].reg == X86_REG_RAX and ins.address+ins.size+ops[0].mem.disp == root):
            if rax is not None:vtables.append(rax)
        else:
            _, written = ins.regs_access()
            if any(ins.reg_name(reg) in ('rax','eax','ax','al','ah') for reg in written) or ins.mnemonic.startswith('call'):
                rax = None
    if (not 1 <= len(vtables) <= 2 or any(not 0 <= v <= image_size-8 for v in vtables)):
        raise TableReadError('Vtable do construtor indisponível, ambígua ou fora da imagem.')
    return {'rva':rva, 'code':raw.hex(), 'root_rva':root, 'vtable_rva':vtables[-1],
            'constructor_vtable_writes':vtables}
