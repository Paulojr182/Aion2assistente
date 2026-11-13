"""Bounded HUD-only attempt tracking, explicitly without item receipt proof."""
import math
from .gather_evidence import object_key
from .native_gather_progress import GatherProgressFrame,GatherResultTracker,active_interactions


class GatherHudAttempt:
    item_receipt_confirmed=False

    def __init__(self,source_id,key,interactions,progress,*,observed_at,now,timeout=15.,max_age=1.):
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (observed_at,now,timeout,max_age)):
            raise ValueError('Prazos da tentativa de coleta inválidos.')
        if timeout<=0 or max_age<=0 or not 0<=now-observed_at<=max_age:
            raise ValueError('Captura inicial de coleta desatualizada.')
        self.scope=self._scope(interactions)
        actions=self._actions(interactions)
        if (not isinstance(progress,GatherProgressFrame) or progress.session!=self.scope[0]
                or progress.stage not in ('hud_absent','progress_absent','progress_hidden')
                or active_interactions(actions)):
            raise ValueError('Tentativa exige HUD ocioso e nenhuma interação ativa.')
        progress.result_kind()
        self.tracker=GatherResultTracker(source_id,object_key(key))
        self.timeout=timeout;self.max_age=max_age
        self.last_observed_at=observed_at;self.submitted_at=None
        self.hud_result=None;self.final=None;self.reason=None

    @staticmethod
    def _scope(report):
        session=report.get('session')
        player=report.get('player_identity');character=report.get('character_session_identity')
        if (not isinstance(session,str) or not session or report.get('player_present') is not True
                or report.get('layout_code_validated') is not True or report.get('receipt_eligible') is not True):
            raise ValueError('Relatório de interação sem código/sessão verificados.')
        for identity in (player,character):
            if (not isinstance(identity,(tuple,list)) or len(identity)!=2
                    or any(type(v) is not int or v<0 for v in identity)):
                raise ValueError('Identidade do personagem inválida.')
        return session,tuple(player),tuple(character)

    @staticmethod
    def _actions(report):
        raw=report.get('actions')
        if not isinstance(raw,(tuple,list)):raise ValueError('Ações de interação indisponíveis.')
        actions=[]
        for action in raw:
            if not isinstance(action,dict) or action.get('layout_code_validated') is not True:
                raise ValueError('Interação sem layout verificado.')
            actions.append({**action,'target':object_key(action.get('target'))})
        active_interactions(actions)
        return tuple(actions)

    def submitted(self,now,*,dispatched_at=None):
        dispatched_at=now if dispatched_at is None else dispatched_at
        if (self.submitted_at is not None or self.final is not None
                or type(now) not in (int,float) or not math.isfinite(now)
                or type(dispatched_at) not in (int,float) or not math.isfinite(dispatched_at)
                or not 0<=dispatched_at-self.last_observed_at<=self.max_age
                or not 0<=now-dispatched_at<=self.max_age):
            raise ValueError('Envio repetido ou captura inicial expirada.')
        self.submitted_at=now

    def _finish(self,result,reason):
        self.final=result;self.reason=reason
        return result

    def observe(self,interactions,progress,*,observed_at,now):
        if self.final is not None:return self.final
        if self.submitted_at is None:raise ValueError('Envio ainda não foi confirmado.')
        if type(now) not in (int,float) or not math.isfinite(now) or now<self.submitted_at:
            return self._finish('unconfirmed','Relógio inválido.')
        if now-self.submitted_at>=self.timeout:
            return self._finish('unconfirmed','Prazo sem término atribuído pelo HUD.')
        if (type(observed_at) not in (int,float) or not math.isfinite(observed_at)
                or not 0<=now-observed_at<=self.max_age or observed_at<=max(self.last_observed_at,self.submitted_at)):
            return 'waiting'
        self.last_observed_at=observed_at
        try:
            if self._scope(interactions)!=self.scope:raise ValueError('Personagem ou sessão mudou.')
            actions=self._actions(interactions)
            if not isinstance(progress,GatherProgressFrame) or progress.session!=self.scope[0]:
                raise ValueError('HUD de outra sessão.')
            if progress.stage not in ('hud_absent','progress_absent','progress_hidden','progress_visible'):
                raise ValueError('Estado do HUD indisponível.')
            if self.hud_result is None:
                self.hud_result=self.tracker.observe(actions,progress)
            else:
                # A final animation can outlive its action; a new action must
                # never inherit that result while waiting for the old one to end.
                for action in active_interactions(actions):
                    identity=(action.get('object_index'),action.get('serial_number'))
                    if action['target']!=self.tracker.key or identity!=self.tracker.action_identity:
                        raise ValueError('Interação substituída após resultado do HUD.')
                result=progress.result_kind()
                if result=='failure':self.hud_result='failure'
                if progress.stage=='progress_visible' and progress.source_id!=self.tracker.source_id:
                    raise ValueError('HUD substituído por outro recurso.')
                if progress.stage=='progress_visible' and progress.finalizing is not True:
                    raise ValueError('HUD reiniciou antes de confirmar o término da ação.')
            if self.hud_result=='failure':return self._finish('hud_failure','HUD nativo indicou falha.')
            if self.hud_result=='success' and not active_interactions(actions):
                return self._finish('hud_success','Término pelo HUD; recebimento de item não observado.')
        except (ValueError,TypeError) as exc:
            return self._finish('unconfirmed',str(exc))
        return 'waiting'
