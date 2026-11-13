"""Checked bag capacity reconstructed from core.bag_capacity.bag_state.

The original compiled helpers identify + at RVA 0x3b98680 and * at
0x3ba0a00 through their operator-specific TypeError strings. The body sums
base/paid/bonus, clamps units to the row maximum, then multiplies by step.
No expansion purchases or currency reads are performed.
"""
import struct
from .inventory_records import InventoryReadError
from .name_pool import pointer


def bag_capacity(base_units,paid,bonus,step,maximum_units):
    values=(base_units,paid,bonus,step,maximum_units)
    if (any(type(value) is not int for value in values)
            or not 0 <= base_units <= 0xffffffff
            or base_units+paid+bonus>0x7fffffff
            or not 0 <= paid <= 255 or not 0 <= bonus <= 255 or paid+bonus > 255
            or not 1 <= step <= 100 or not 1 <= maximum_units <= 255):
        raise InventoryReadError('Contadores, passo ou máximo da mochila inválidos.')
    return {'base_units':base_units,'paid':paid,'bonus':bonus,'step':step,
            'maximum_units':maximum_units,'maximum':maximum_units*step,
            'capacity':min(base_units+paid+bonus,maximum_units)*step}


def accessor_root(rva,raw,image_size):
    if type(raw) is not bytes or len(raw)<96 or raw[89:92]!=b'\x48\x8d\x1d':
        raise InventoryReadError('Referência do acessor da mochila mudou.')
    root=rva+96+struct.unpack_from('<i',raw,92)[0]
    if not 0 <= root <= image_size-536:
        raise InventoryReadError('Raiz da mochila fora da imagem.')
    return root


def read_bag_capacity(read,session,config,table,*,max_bytes=8192):
    for address in (session,config,table):pointer(address)
    if type(max_bytes) is not int or not 128 <= max_bytes <= 65536:
        raise InventoryReadError('Orçamento da mochila inválido.')
    journal={};used=0
    def capture(address,size):
        nonlocal used
        pointer(address)
        if not 0 < size <= max_bytes-used:raise InventoryReadError('Orçamento da mochila excedido.')
        used+=size
        raw=read(address,size)
        if type(raw) is not bytes or len(raw)!=size:raise InventoryReadError('Leitura da mochila incompleta.')
        key=address,size
        if key in journal and journal[key]!=raw:raise InventoryReadError('Mochila mudou durante a leitura.')
        journal[key]=raw
        return raw
    paid,bonus=struct.unpack('<BB',capture(session+1872,2))
    base,step=struct.unpack('<II',capture(config+528,8))
    storage,count,capacity=struct.unpack('<Qii',capture(table+96,16))
    if count!=1 or not count <= capacity <= 1000000:
        raise InventoryReadError('Tabela de expansão ausente, ambígua ou inválida.')
    record,control=struct.unpack('<QQ',capture(storage,16))
    pointer(record);pointer(control)
    if struct.unpack('<i',capture(control+8,4))[0] <= 0:
        raise InventoryReadError('Referência da tabela de expansão expirou.')
    maximum=struct.unpack('<H',capture(record+16,2))[0]
    result=bag_capacity(base,paid,bonus,step,maximum)
    for (address,size),expected in journal.items():
        if size > max_bytes-used:raise InventoryReadError('Orçamento de conferência da mochila excedido.')
        used+=size
        if read(address,size)!=expected:raise InventoryReadError('Mochila mudou antes da publicação.')
    return result
