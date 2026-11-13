"""Produce collection evidence from consistent, current native observations."""
from collections import OrderedDict
from dataclasses import dataclass
import math
from .gather_identity import format_gather_identity
from .gather_receipt import GatherEvidence
from .inventory_records import ITEM_DOMAINS
from .native_gather_progress import GatherProgressFrame,active_interactions


def object_key(value):
    if (not isinstance(value,(tuple,list)) or len(value)!=3
            or any(type(part) is not int for part in value)):
        raise ValueError('Chave completa do recurso indisponível.')
    tag,payload,extra=value
    if tag not in (1,2) or not 0<payload<2**64 or not 0<=extra<=255 or tag==1 and payload>0xffffffff:
        raise ValueError('Chave completa do recurso inválida.')
    return tuple(value)


@dataclass(frozen=True)
class GatherSourceIdentity:
    identity: str
    key: tuple[int,int,int]
    source_id: int


class GatherSourceHistory:
    """Bounded identity history for receipts after a collected actor disappears."""
    def __init__(self,limit=256):
        if type(limit) is not int or not 0<limit<=1024:raise ValueError('Limite do histórico inválido.')
        self.limit=limit;self.scope=None;self.sources=OrderedDict()

    def observe(self,scope,raw_targets):
        current=[];keys=set();identities=set()
        for entry in raw_targets:
            identity=format_gather_identity(entry);key=object_key(entry.get('object_key'))
            if int(entry['environment_key'])!=key[1] or key in keys or identity in identities:
                raise ValueError('Chave de recurso divergente ou ambígua na cena.')
            keys.add(key);identities.add(identity)
            current.append(GatherSourceIdentity(identity,key,entry['source']['id']))
        if scope!=self.scope:
            self.sources.clear();self.scope=scope
        for source in current:
            for old_id,old in tuple(self.sources.items()):
                if old.key==source.key and old_id!=source.identity:del self.sources[old_id]
            self.sources.pop(source.identity,None)
            self.sources[source.identity]=source
        while len(self.sources)>self.limit:self.sources.popitem(last=False)
        return tuple(self.sources.values())


def inventory_totals(report):
    domains=report.get('observed_domains')
    if (report.get('read_consistency_verified') is not True
            or report.get('inventory_capacity_observed') is not True
            or report.get('primary_bag_domain_classified') is not True
            or report.get('primary_bag_domain')!=1 or report.get('items_truncated') is True
            or not isinstance(domains,(tuple,list)) or len(domains)!=len(ITEM_DOMAINS)
            or any(type(domain) is not int for domain in domains)
            or set(domains)!={d for d,_ in ITEM_DOMAINS}
            or not isinstance(report.get('items'),(tuple,list))):
        raise ValueError('Inventário/capacidade da coleta não foram verificados integralmente.')
    capacity,used,free=(report.get(name) for name in ('capacity','used_slots','free_slots'))
    if (any(type(value) is not int for value in (capacity,used,free))
            or not 0<=used<=capacity<=25500 or free!=capacity-used):
        raise ValueError('Ocupação ou espaço livre da mochila inconsistentes.')
    totals={};seen=set();bag_count=0
    for row in report['items']:
        instance,definition,count,domain=(row.get(name) for name in ('instance_id','definition_id','quantity','domain'))
        if (any(type(value) is not int for value in (instance,definition,count,domain))
                or instance<=0 or instance in seen or not 0<definition<=0xffffffff
                or not 0<=count<=10**15 or domain not in {d for d,_ in ITEM_DOMAINS}):
            raise ValueError('Registros de inventário inválidos para confirmar coleta.')
        seen.add(instance);bag_count+=domain==1
        totals[definition]=totals.get(definition,0)+count
    if bag_count!=used:raise ValueError('A ocupação não corresponde aos itens da mochila.')
    return tuple(sorted(totals.items()))


def build_gather_evidence(snapshot,process_session,source_report,inventory,interactions,progress,sources):
    if (type(process_session) is not str or not process_session or not snapshot.scope
            or not math.isfinite(snapshot.captured_at)):
        raise ValueError('Escopo ou horário da captura inválido para coleta.')
    reports=(source_report,inventory,interactions)
    character=source_report.get('character_session_identity')
    if not isinstance(character,(tuple,list)) or len(character)!=2:
        raise ValueError('Identidade da sessão do personagem indisponível.')
    for report in reports:
        if (report.get('player_present') is not True or report.get('session')!=process_session
                or tuple(report.get('player_identity',()))!=snapshot.player_identity
                or tuple(report.get('character_session_identity') or ())!=tuple(character)):
            raise ValueError('Observações de coleta pertencem a sessões/personagens diferentes.')
    if (interactions.get('receipt_eligible') is not True or interactions.get('layout_code_validated') is not True
            or not isinstance(progress,GatherProgressFrame) or progress.session!=process_session):
        raise ValueError('Interação/progresso sem validação da sessão atual.')
    result=progress.result_kind()
    actions=[]
    for action in interactions.get('actions',()):
        if action.get('layout_code_validated') is not True:
            raise ValueError('Ciclo de interação sem código verificado.')
        actions.append({**action,'target':object_key(action.get('target'))})
    active=active_interactions(actions)
    if len(active)>1:raise ValueError('Mais de uma interação ativa impede atribuição de coleta.')
    owner_key=None;interaction_id=None
    if active:
        action=active[0];owner_key=action['target']
        interaction_id=action.get('object_index'),action.get('serial_number')
        if (any(type(value) is not int for value in interaction_id)
                or interaction_id[0]<0 or not 0<interaction_id[1]<=0xffffffff):
            raise ValueError('Identidade nativa da interação ativa inválida.')
    sources=tuple(sources)
    if len({source.key for source in sources})!=len(sources):
        raise ValueError('Histórico de recursos possui chave ambígua.')
    owner=next((source.identity for source in sources if source.key==owner_key),None)
    if active and owner is None:owner='other:'+':'.join(str(part) for part in owner_key)
    totals=inventory_totals(inventory)
    evidence=[]
    for source in sources:
        visible=progress.stage=='progress_visible'
        matching_progress=visible and progress.source_id==source.source_id
        if owner==source.identity and visible and not matching_progress:
            raise ValueError('O progresso visível pertence a outra definição de recurso.')
        evidence.append(GatherEvidence(snapshot.scope,source.identity,snapshot.captured_at,totals,True,
            owner,matching_progress,bool(active),interaction_id,
            result if matching_progress else None))
    return tuple(evidence),totals
