"""Owned native macro lease, heartbeat and target commands. No desktop input."""
import hashlib,threading,time
from .background_probe import probe_thread
from .macro_loop import MacroNotReady,TargetChanged
from .native_macro_cycle import (make_macro_cycle_packet,make_macro_lease_packet,make_macro_lease_control_packet,
                                 make_next_target_packet,make_collect_packet)
from .native_movement import make_movement_packet
from .native_flight_packet import make_flight_toggle_packet,make_flight_movement_packet
from .native_auto_move import make_auto_move_packet
from .native_autorun import make_autorun_packet


class MacroTransport:
    def __init__(self,reader):
        self.reader=reader
        artifacts=reader.load('native/probe_build.json')['artifacts']
        artifact=artifacts['macro_cycle']
        self.dll=reader.base/'native'/artifact['filename'];self.digest=artifact['sha256']
        if not self.dll.is_file() or hashlib.sha256(self.dll.read_bytes()).hexdigest()!=self.digest:
            raise RuntimeError('Componente de combate ausente ou diferente da compilação verificada.')
        mov_artifact=artifacts.get('movement_cycle')
        self.movement_dll=(reader.base/'native'/mov_artifact['filename']) if mov_artifact else None
        self.movement_digest=mov_artifact['sha256'] if mov_artifact else None
        self.flight_artifacts={}
        for name in ('flight_toggle','flight_movement','auto_move','autorun_toggle'):
            if name in artifacts:
                item=artifacts[name];path=reader.base/'native'/item['filename']
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
                    raise RuntimeError('Componente de voo não coincide com a compilação verificada.')
                self.flight_artifacts[name]=(path,item['sha256'])
        if self.movement_dll is not None and (not self.movement_dll.is_file() or hashlib.sha256(self.movement_dll.read_bytes()).hexdigest()!=self.movement_digest):
            raise RuntimeError('Componente de movimento ausente ou diferente da compilação verificada.')
        self.frame=None;self.frame_scope=None
        self.collect_frame=None;self.collect_scope=None
        self.target_frame=None;self.target_scope=None
        self.movement_profile_cache=None;self.movement_scope=None
        self.worker=None;self.result=None;self.error=None
        self.posted=threading.Event();self.abort=threading.Event();self.done=threading.Event()
        self.last_snapshot=None;self.last_off_at=-1.0
        self.stats_lock=threading.Lock();self.root_acknowledged=False
        self.stats={'macro_starts':0,'renewals':0,'stops':0,'target_commands':0,'collect_commands':0,'background_collect_commands':0,'movement_pulses':0,'background_samples':0,'foreground_samples':0}
        self.stats.update(flight_takeoffs=0,flight_pulses=0,flight_callback_calls=0)

    def telemetry(self):
        with self.stats_lock:return dict(self.stats)

    def validate(self,*,use_hook=True):
        frame=self.reader.bindings()
        packet=make_macro_cycle_packet(frame,session=self.reader.session)
        if use_hook:
            reply=self._run(packet)
            if reply['probe_result']!=0:raise RuntimeError('A conexão de comandos com o jogo não foi validada.')
        return True

    def snapshot(self):
        self.last_snapshot=self.reader.snapshot()
        return self.last_snapshot

    def _frame(self,snapshot,action='Macro'):
        def validate(frame):
            objects=frame['frame_objects']
            for role,expected in (('pawn',snapshot.player_identity),('input_target',snapshot.input_identity)):
                if expected is None or (objects[role]['index'],objects[role]['serial'])!=expected:
                    raise TargetChanged('O personagem mudou antes do comando.')
            return frame
        if action=='Macro':
            if self.frame is None or self.frame_scope!=snapshot.scope:
                self.frame=self.reader.bindings();self.frame_scope=snapshot.scope
            return validate(self.frame)
        if action=='Action':
            if self.collect_frame is None or self.collect_scope!=snapshot.scope:
                self.collect_frame=self.reader.bindings('Action')
                self.collect_scope=snapshot.scope
            return validate(self.collect_frame)
        if self.target_frame is None or self.target_scope!=snapshot.scope:
            self.target_frame=self.reader.bindings('Combat_NextTarget');self.target_scope=snapshot.scope
        return validate(self.target_frame)

    def _run(self,packet,*,persistent=False):
        with self.reader.process() as process:
            reply=probe_thread(process,self.reader.window,self.dll,self.digest,packet=packet,
                protocol='macro_cycle',completion_timeout=None if persistent else 3,
                abort_event=self.abort if persistent else None,
                dispatch_event=self.posted if persistent else None)
        with self.stats_lock:
            if packet.mode in (3,5,6,7) and reply['probe_result']==0:
                field='background_samples' if not reply['game_window_was_foreground'] and not reply['game_window_is_foreground'] else 'foreground_samples'
                self.stats[field]+=1
            if reply.get('macro_lease_renewed'):
                self.stats['renewals']+=1
                if not self.root_acknowledged:self.root_acknowledged=True;self.stats['macro_starts']+=1
            if reply.get('macro_lease_completed') and not self.root_acknowledged:
                self.root_acknowledged=True;self.stats['macro_starts']+=1
            if reply.get('macro_stop_confirmed'):self.stats['stops']+=1
            if reply.get('next_target_dispatched'):self.stats['target_commands']+=1
        return reply

    def _control(self,snapshot,operation):
        frame=self._frame(snapshot)
        packet=make_macro_lease_control_packet(frame,session=self.reader.session,operation=operation)
        return self._run(packet)

    def _start(self,snapshot):
        self.reader.require_target(snapshot)
        frame=self._frame(snapshot)
        packet=make_macro_lease_packet(frame,session=self.reader.session,execute=True)
        self.result=None;self.error=None
        with self.stats_lock:self.root_acknowledged=False
        self.posted.clear();self.abort.clear();self.done.clear()
        def run():
            try:self.result=self._run(packet,persistent=True)
            except Exception as exc:self.error=f'{type(exc).__name__}: {exc}'
            finally:self.done.set()
        self.worker=threading.Thread(target=run,name='AionNativeMacroLease',daemon=True)
        self.worker.start()

    def ensure_macro(self,snapshot):
        if time.monotonic()-snapshot.captured_at>0.75:raise RuntimeError('Alvo desatualizado antes do macro.')
        if self.worker is None or self.done.is_set():
            if self.error:raise RuntimeError(self.error)
            if self.result is not None and self.result['probe_result']!=0:
                raise RuntimeError('A execução nativa do macro não terminou de forma confirmada.')
            self._start(snapshot)
        deadline=time.monotonic()+1
        while time.monotonic()<deadline:
            if self.done.is_set():
                if (self.result is not None and self.result['probe_result']==29
                    and self.result.get('macro_state_trace')==[False,False,False]):
                    self.worker=None;self.result=None;self.last_off_at=time.monotonic()
                    self.reader.require_target(snapshot)
                    raise MacroNotReady('O jogo ainda não permite iniciar o macro.')
                if self.result is not None and self.result.get('macro_state_trace',[None,None,None])[2] is False:
                    try:self.reader.require_target(snapshot)
                    except TargetChanged:
                        self.worker=None;self.result=None;self.last_off_at=time.monotonic()
                        raise
                r=self.result or {}
                raise RuntimeError(self.error or ('O macro não permaneceu ativo no jogo '
                    f"(resultado={r.get('probe_result')}, etapa={r.get('failure_stage')}, estado={r.get('macro_state_trace')})."))
            if not self.posted.wait(0.02):continue
            reply=self._control(snapshot,'renew')
            if reply.get('macro_lease_renewed'):return True
            if reply['probe_result']==32:
                # The game's own macro may reach its native duration limit.
                self.stop_macro(force=True)
                self._start(self.reader.require_target(snapshot))
            elif reply['probe_result']!=33:
                raise RuntimeError(f"Renovação do macro não confirmada ({reply['probe_result']}).")
            time.sleep(0.02)
        raise RuntimeError('O jogo não confirmou a manutenção do macro.')

    def stop_macro(self,*,force=False):
        snapshot=self.last_snapshot
        if self.worker is not None and not self.done.is_set():
            self.posted.wait(1)
            if not self.posted.is_set():
                self.abort.set();self.worker.join(1)
                if not self.done.is_set():raise RuntimeError('Cancelamento do macro ainda não confirmado.')
            else:
                if snapshot is None:raise RuntimeError('Sessão de parada indisponível.')
                reply=self._control(snapshot,'stop')
                if not reply.get('macro_stop_confirmed'):
                    self.abort.set()
                    raise RuntimeError('O jogo ainda não confirmou o macro desligado.')
                self.worker.join(1)
                if not self.done.is_set():raise RuntimeError('O período nativo do macro ainda está encerrando.')
            self.last_off_at=time.monotonic()
        if self.done.is_set() and self.worker is not None:
            if self.error:raise RuntimeError(self.error)
            if self.result is None or self.result['probe_result']!=0 or self.result['macro_state_trace'][2] is not False:
                r=self.result or {}
                raise RuntimeError('Parada do período nativo não confirmada '
                    f"(resultado={r.get('probe_result')}, etapa={r.get('failure_stage')}, estado={r.get('macro_state_trace')}).")
            self.worker=None;self.result=None
            self.last_off_at=time.monotonic()
        if (force and snapshot is not None and snapshot.macro_active
            and snapshot.captured_at>self.last_off_at):
            reply=self._control(snapshot,'stop_current')
            if not reply.get('macro_stop_confirmed'):raise RuntimeError('Parada do macro atual não confirmada.')
            self.last_off_at=time.monotonic()
        return True

    def collect(self, snapshot):
        self.stop_macro(force=True)
        frame=self._frame(snapshot,'Action')
        reply=self._run(make_collect_packet(frame,session=self.reader.session))
        if not reply.get('collect_dispatched'):
            raise OSError('A ação interna de coleta foi recusada.')
        with self.stats_lock:
            self.stats['collect_commands']+=1
            if not reply['game_window_was_foreground'] and not reply['game_window_is_foreground']:
                self.stats['background_collect_commands']+=1
        return True

    def _movement_profile(self,snapshot):
        if self.movement_profile_cache is None or self.movement_scope!=snapshot.scope:
            self.movement_profile_cache=self.reader.movement_profile()
            self.movement_scope=snapshot.scope
        return self.movement_profile_cache

    def movement_pulse(self,snapshot,direction,*,duration_ms=100,execute=True,stop_macro=True):
        if self.movement_dll is None:
            raise RuntimeError('DLL de movimento não configurada.')
        if stop_macro:
            self.stop_macro(force=True)
        frame=self._frame(snapshot,'Action')
        profile=self._movement_profile(snapshot)
        packet=make_movement_packet(frame,profile,session=self.reader.session,
                                    direction=direction,duration_ms=duration_ms,execute=execute)
        with self.reader.process() as process:
            reply=probe_thread(process,self.reader.window,self.movement_dll,self.movement_digest,
                               packet=packet,protocol='movement',completion_timeout=3)
        if execute:
            if not reply.get('movement_pulse_completed'):
                raise OSError('O pulso nativo de movimento não foi confirmado.')
            with self.stats_lock:
                self.stats['movement_pulses']+=1
        elif not reply.get('movement_validated'):
            raise RuntimeError('A validação nativa de movimento falhou.')
        return reply

    @property
    def supports_auto_move(self):
        return 'auto_move' in self.flight_artifacts

    def auto_move(self,snapshot,goal,*,execute=True,map_mode=False,plan=False,flag=None,flag_only=False,map_position=False,map_stop=False,map_flight=False,map_manager=False):
        """Ask the game to path to goal itself (same as map right-click)."""
        if 'auto_move' not in self.flight_artifacts:raise RuntimeError('Componente de deslocamento automático ausente.')
        self.stop_macro(force=True)
        frame=self._frame(snapshot,'Action')
        packet=make_auto_move_packet(frame,session=self.reader.session,goal=goal,execute=execute,map_mode=map_mode,plan=plan,flag=flag,flag_only=flag_only,map_position=map_position,map_stop=map_stop,map_flight=map_flight,map_manager=map_manager)
        dll,digest=self.flight_artifacts['auto_move']
        with self.reader.process() as process:
            reply=probe_thread(process,self.reader.window,dll,digest,packet=packet,protocol='auto_move',completion_timeout=3)
        if execute and not reply.get('auto_move_dispatched'):
            raise RuntimeError(f"Deslocamento automático recusado (resultado={reply['probe_result']}, etapa={reply.get('auto_move_stage')}, guarda={reply.get('failure_stage')}, estado_anterior={reply.get('auto_move_mode_before')}).")
        if execute:
            with self.stats_lock:self.stats['auto_moves']=self.stats.get('auto_moves',0)+1
        return reply

    def plan_route(self,snapshot,goal):
        """Game's own route to goal (navmesh points), cancelled before any movement."""
        reply=self.auto_move(snapshot,goal,execute=True,plan=True)
        return reply.get('route_points') or []

    def button_action(self,snapshot,button,*,release=False,execute=True):
        """Game's own button action (Move_Ride, Move_Sprint press/release, Move_AutoForward)."""
        if 'autorun_toggle' not in self.flight_artifacts:raise RuntimeError('Componente de ações de movimento ausente.')
        bindings=self.reader.bindings(button)
        pawn=bindings['frame_objects']['pawn']
        if (pawn['index'],pawn['serial'])!=snapshot.player_identity:raise TargetChanged('Personagem mudou antes da ação.')
        packet=make_autorun_packet(bindings,session=self.reader.session,execute=execute,button=button,release=release)
        dll,digest=self.flight_artifacts['autorun_toggle']
        with self.reader.process() as process:
            reply=probe_thread(process,self.reader.window,dll,digest,packet=packet,protocol='autorun')
        if execute and not reply.get('autorun_dispatched'):
            raise RuntimeError(f"Ação {button} recusada (resultado={reply['probe_result']}, etapa={reply.get('autorun_stage')}, guarda={reply.get('failure_stage')}, estado_anterior={reply.get('auto_move_mode_before')}).")
        return reply

    def skill_press(self,snapshot,slot,*,hold=0.08):
        """Aperta e solta a skill do slot 1..8 da barra (acao normal QuickSlot_Skill do jogo)."""
        if type(slot) is not int or not 1<=slot<=8:raise ValueError('Slot de skill deve ser 1..8.')
        if 'autorun_toggle' not in self.flight_artifacts:raise RuntimeError('Componente de acoes ausente.')
        cache=self.__dict__.setdefault('_skill_bindings',{})
        bindings=cache.get(slot)
        if bindings is None or bindings.get('frame_scope',{}).get('session')!=self.reader.session:
            bindings=cache[slot]=self.reader.bindings('QuickSlot_Skill',number=slot+1)
        pawn=bindings['frame_objects']['pawn']
        if (pawn['index'],pawn['serial'])!=snapshot.player_identity:
            cache.pop(slot,None);raise TargetChanged('Personagem mudou antes da skill.')
        dll,digest=self.flight_artifacts['autorun_toggle']
        replies=[]
        for release in (False,True):
            packet=make_autorun_packet(bindings,session=self.reader.session,execute=True,button='QuickSlot_Skill',release=release)
            try:
                with self.reader.process() as process:
                    replies.append(probe_thread(process,self.reader.window,dll,digest,packet=packet,protocol='autorun'))
            except Exception:
                cache.pop(slot,None);raise
            if not release:time.sleep(hold)
        if not replies[0].get('autorun_dispatched'):
            cache.pop(slot,None)
            raise RuntimeError(f"Skill {slot} recusada (resultado={replies[0].get('probe_result')}, guarda={replies[0].get('failure_stage')}).")
        return True

    def autorun_toggle(self,snapshot,*,execute=True):
        """Press the game's own auto-run action once (toggles running forward)."""
        if 'autorun_toggle' not in self.flight_artifacts:raise RuntimeError('Componente de corrida automática ausente.')
        self.stop_macro(force=True)
        bindings=self.reader.bindings('Move_AutoForward')
        pawn=bindings['frame_objects']['pawn']
        if (pawn['index'],pawn['serial'])!=snapshot.player_identity:raise TargetChanged('Personagem mudou antes da corrida.')
        packet=make_autorun_packet(bindings,session=self.reader.session,execute=execute)
        dll,digest=self.flight_artifacts['autorun_toggle']
        with self.reader.process() as process:
            reply=probe_thread(process,self.reader.window,dll,digest,packet=packet,protocol='autorun')
        if execute and not reply.get('autorun_dispatched'):
            raise RuntimeError(f"Corrida automática recusada (resultado={reply['probe_result']}, etapa={reply.get('autorun_stage')}, guarda={reply.get('failure_stage')}, estado_anterior={reply.get('auto_move_mode_before')}).")
        return reply

    def flight_takeoff(self,snapshot):
        self.stop_macro(force=True)
        if 'flight_toggle' not in self.flight_artifacts:raise RuntimeError('Componente de decolagem ausente.')
        bindings=self.reader.bindings('Move_Fly')
        pawn=bindings['frame_objects']['pawn']
        if (pawn['index'],pawn['serial'])!=snapshot.player_identity:raise TargetChanged('Personagem mudou antes da decolagem.')
        state=self.reader.flight_state()
        packet=make_flight_toggle_packet(bindings,state,session=self.reader.session,execute=True)
        dll,digest=self.flight_artifacts['flight_toggle']
        with self.reader.process() as process:
            reply=probe_thread(process,self.reader.window,dll,digest,packet=packet,protocol='flight')
        with self.stats_lock:
            self.stats['flight_callback_calls']+=reply.get('flight_callback_calls',0)
            self.stats['flight_takeoffs']+=int(reply.get('flight_action_dispatched') is True)
        if not reply.get('flight_action_dispatched'):raise RuntimeError('A decolagem normal não foi enviada.')
        deadline=time.monotonic()+2.
        while time.monotonic()<deadline:
            current=self.reader.flight_state()
            if (current['session']!=state['session'] or current['player_identity']!=state['player_identity']
                    or current['movement']['identity']!=state['movement']['identity']):
                raise TargetChanged('Personagem/componente mudou durante a decolagem.')
            if current['movement']['MovementMode']==5:return True
            time.sleep(.05)
        raise RuntimeError('Decolagem enviada sem transição para voo; nenhuma repetição automática.')

    def flight_land(self,snapshot,timeout=8.0):
        """Mesma acao normal Move_Fly (tecla V) usada em voo: encerra o voo e desce ao chao."""
        self.stop_macro(force=True)
        if 'flight_toggle' not in self.flight_artifacts:raise RuntimeError('Componente de voo ausente.')
        bindings=self.reader.bindings('Move_Fly')
        state=self.reader.flight_state()
        if state['movement']['MovementMode']!=5:return True
        packet=make_flight_toggle_packet(bindings,state,session=self.reader.session,execute=True,land=True)
        dll,digest=self.flight_artifacts['flight_toggle']
        with self.reader.process() as process:
            reply=probe_thread(process,self.reader.window,dll,digest,packet=packet,protocol='flight')
        if not reply.get('flight_action_dispatched'):raise RuntimeError(f"Pouso (V) nao enviado (resultado={reply.get('probe_result')}).")
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if self.reader.flight_state()['movement']['MovementMode']!=5:return True
            time.sleep(.1)
        return False

    def flight_movement_pulse(self,snapshot,direction,*,duration_ms=100,execute=True):
        self.stop_macro(force=True)
        if 'flight_movement' not in self.flight_artifacts:raise RuntimeError('Componente aéreo ausente.')
        frame=self._frame(snapshot,'Action');profile=self._movement_profile(snapshot)
        state=self.reader.flight_state()
        packet=make_flight_movement_packet(frame,profile,state,session=self.reader.session,
            direction=direction,duration_ms=duration_ms,execute=execute)
        dll,digest=self.flight_artifacts['flight_movement']
        with self.reader.process() as process:
            reply=probe_thread(process,self.reader.window,dll,digest,packet=packet,protocol='flight_movement')
        with self.stats_lock:
            self.stats['flight_callback_calls']+=reply.get('movement_callbacks',0)
            self.stats['flight_pulses']+=int(reply.get('movement_callbacks',0)>0)
        if execute and not reply.get('movement_pulse_completed'):raise RuntimeError('Pulso aéreo não confirmado.')
        return reply

    def flight_movement_stream(self,snapshot,direction,*,lease_ms=700,stop=False):
        raise RuntimeError('Voo continuo desativado apos retorno de posicao; usar controle por pulsos.')

    def _flight_movement_stream_experimental(self,snapshot,direction,*,lease_ms=700,stop=False):
        """Renew or release a short owned input lease on the game thread."""
        if 'flight_movement' not in self.flight_artifacts:
            raise RuntimeError('Componente aereo ausente.')
        frame=self._frame(snapshot,'Action');profile=self._movement_profile(snapshot)
        state=self.reader.flight_state()
        packet=make_flight_movement_packet(frame,profile,state,session=self.reader.session,
            direction=direction,duration_ms=lease_ms,execute=True,stream=True,stream_stop=stop)
        dll,digest=self.flight_artifacts['flight_movement']
        with self.reader.process() as process:
            reply=probe_thread(process,self.reader.window,dll,digest,packet=packet,protocol='flight_movement')
        if not reply.get('flight_stream_acknowledged'):
            raise RuntimeError('Fluxo aereo nao confirmado: '+str(reply.get('probe_result')))
        return reply

    def next_target(self,snapshot,*,skip_current=False):
        self.stop_macro(force=True)
        frame=self._frame(snapshot,'Combat_NextTarget')
        current=self.reader.snapshot()
        target=current.target
        same_rejected_target = (skip_current and snapshot.target is not None and target is not None
                                and target.identity == snapshot.target.identity)
        if (current.scope!=snapshot.scope or current.player_health<=0
                or (target is not None and target.eligible and target.health>0 and target.maximum>0
                    and not same_rejected_target)):
            raise TargetChanged('Um alvo válido apareceu antes da troca.')
        reply=self._run(make_next_target_packet(frame,session=self.reader.session,execute=True))
        if not reply.get('next_target_dispatched'):raise RuntimeError('Busca do próximo inimigo não confirmada.')
        return True

    def close(self):
        try:self.stop_macro(force=False)
        finally:
            self.abort.set()
            if self.worker is not None:self.worker.join(1)
