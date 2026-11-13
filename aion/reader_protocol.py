"""Fixed read-only requests; no addresses, paths, expressions or shell commands."""
import json,re
from .activity_readback import ACTIVITY_READ_TASKS

TASKS=frozenset(('status','metadata','metadata_wide','player','scene','positions','resource_layout','resources','npc_schema','npcs','target','combat_schema','input_schema','macro_schema','macro_snapshot','macro_compare','action_bindings','stop'))
TASKS=TASKS | ACTIVITY_READ_TASKS


def parse_request(raw):
    if type(raw) is not bytes or not 1<=len(raw)<=1024:
        raise ValueError('Tamanho de solicitação inválido.')
    data=json.loads(raw.decode('utf-8'))
    if (not isinstance(data,dict) or set(data)!= {'id','task'}
            or type(data['id']) is not str or not re.fullmatch('[0-9a-f]{32}',data['id'])
            or type(data['task']) is not str or data['task'] not in TASKS):
        raise ValueError('Solicitação fora do protocolo de leitura.')
    return data
