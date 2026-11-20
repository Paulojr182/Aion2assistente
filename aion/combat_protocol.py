"""Separate closed control channel. The existing diagnostic pipe stays read-only."""
import json,re,uuid
from .local_pipe import PipeAPI

SCAN_TASKS = {'scan_targets': None, 'scan_mobs': 'combat', 'scan_gatherables': 'gathering'}
TASKS=frozenset(('status','start','start_gather','start_quests','stop','shutdown', *SCAN_TASKS))

def parse_request(raw):
    if type(raw) is not bytes or not 1<=len(raw)<=512:raise ValueError('Solicitação de combate inválida.')
    value=json.loads(raw.decode('utf-8'))
    if (type(value) is not dict or set(value)!={'id','task'} or type(value['id']) is not str
        or not re.fullmatch('[0-9a-f]{32}',value['id']) or type(value['task']) is not str or value['task'] not in TASKS):
        raise ValueError('Solicitação fora do protocolo de combate.')
    return value

def request(name,task):
    if task not in TASKS:raise ValueError('Comando de combate inválido.')
    api=PipeAPI(kind='combat');channel=api.connect_client(name)
    try:
        identifier=uuid.uuid4().hex
        api.send(channel,json.dumps({'id':identifier,'task':task}).encode('utf-8'))
        response=json.loads(api.receive(channel,65536,timeout=30 if task=='scan_targets' else 6).decode('utf-8'))
        if response.get('id')!=identifier:raise RuntimeError('Resposta de outro comando.')
        api.send(channel,b'ACK')
        if response.get('ok') is not True:raise RuntimeError(response.get('error','Comando não confirmado.'))
        return response['data']
    finally:api.k.CloseHandle(channel)
