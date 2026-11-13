"""Serialized combat lifecycle for a future verified live provider.

No threads are created here. The caller schedules tick; only the provider can
perform game IO. Missing live capabilities block start rather than simulate IO.
"""
import threading,time
from .core import CombatPolicy,Mode,Observation,Command
from .provider import IntegrationUnavailable,ProviderCapabilities


class StopUnconfirmed(RuntimeError):
    pass


class CombatRunner:
    def __init__(self,provider,policy=None,*,clock=time.monotonic):
        self.provider=provider
        self.policy=policy if policy is not None else CombatPolicy()
        self.clock=clock
        self.closed=False;self.started=False;self.last_error=None
        self.lock=threading.RLock()

    @property
    def quiescent(self):
        return not self.policy.running and not self.policy.macro_on and self.policy.pending is None

    def start(self):
        with self.lock:
            if self.closed:raise RuntimeError('Controle encerrado.')
            if self.policy.mode not in (Mode.AUTO_ATTACK,Mode.SEMI_MANUAL):
                raise IntegrationUnavailable('A rotina completa de farm ainda não foi implementada.')
            caps=self.provider.capabilities()
            if not isinstance(caps,ProviderCapabilities) or not (
                    caps.observation and caps.macro_control and caps.macro_state):
                raise IntegrationUnavailable('O provedor ainda não confirma leitura e controle do macro.')
            if self.policy.mode==Mode.AUTO_ATTACK and not caps.selection:
                raise IntegrationUnavailable('Seleção de alvos ainda indisponível.')
            if self.policy.skills and not caps.skills:
                raise IntegrationUnavailable('Execução de habilidades ainda indisponível.')
            state=self.provider.macro_state(session=None)
            if state is not False:
                raise IntegrationUnavailable('Confirme o macro desligado antes de iniciar.')
            self.policy.start();self.started=True;self.last_error=None

    def stop(self):
        with self.lock:self.policy.stop()

    def tick(self):
        with self.lock:
            if self.closed:raise RuntimeError('Controle encerrado.')
            if not self.started or self.quiescent:return None
            try:
                obs=self.provider.observe()
                if not isinstance(obs,Observation):
                    raise TypeError('O provedor não retornou uma observação de combate.')
                if obs.background_input_ready:
                    capabilities=self.provider.capabilities()
                    if not isinstance(capabilities,ProviderCapabilities) or capabilities.background_commands is not True:
                        raise IntegrationUnavailable('O provedor não confirma comandos internos em segundo plano.')
            except (OSError,ValueError,TypeError,RuntimeError) as exc:
                self.last_error=str(exc);self.policy.stop()
                self.policy.status='Leitura indisponível; estado do macro preservado para confirmação'
                return None
            if self.policy.pending==Command('verify_macro_state'):
                if self.policy.session is not None and obs.session!=self.policy.session:
                    self.policy.status='Sessão mudou; estado anterior do macro ainda não confirmado'
                    return None
                try:state=self.provider.macro_state(session=obs.session)
                except (OSError,ValueError,RuntimeError) as exc:
                    self.last_error=str(exc);return None
                if type(state) is not bool:return None
                self.policy.confirm_macro_state(state)
            command=self.policy.step(obs,self.clock())
            if command is None:return None
            try:
                confirmed=self.provider.execute(command,session=obs.session,
                    macro_key=self.policy.macro_key,expected_target=obs.selected)
                if type(confirmed) is not bool:
                    raise ValueError('O provedor não confirmou o resultado do comando.')
            except (OSError,ValueError,RuntimeError) as exc:
                confirmed=False;self.last_error=str(exc)
            self.policy.acknowledge(command,confirmed,self.clock())
            return command

    def close(self):
        with self.lock:
            if self.closed:return
            self.stop()
            if not self.quiescent:self.tick()
            if not self.quiescent:
                raise StopUnconfirmed('O desligamento do macro ainda não foi confirmado.')
            self.provider.close();self.closed=True
