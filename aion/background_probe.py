"""Closed native probe protocols: read-only delivery or explicit Macro cycle."""
import ctypes as C
from ctypes import wintypes as W
import hashlib,secrets,time
from pathlib import Path
from .local_pipe import PipeAPI,SecurityAttributes

MAGIC=0x3145424f52504941


class ProbePacket(C.Structure):
    _fields_=[('magic',C.c_uint64),('version',C.c_uint32),('size',C.c_uint32),('state',C.c_int32),
        ('expected_pid',C.c_uint32),('expected_tid',C.c_uint32),('result',C.c_uint32),
        ('nonce',C.c_uint64),('expiry',C.c_uint64),('expected_birth',C.c_uint64),('expected_base',C.c_uint64),
        ('expected_image_size',C.c_uint64),('observed_birth',C.c_uint64),('observed_base',C.c_uint64),
        ('observed_image_size',C.c_uint64),('observed_pid',C.c_uint32),('observed_tid',C.c_uint32)]


def probe_thread(process,window,dll,digest,*,packet=None,protocol='read_only',require_background=False,
                 completion_timeout=3,abort_event=None,dispatch_event=None):
    if protocol=='read_only':
        protocol_version=1
        expected_size=104;magic=MAGIC;prefix='AionRebuildProbe-v1';message_name='AionRebuild.ReadOnlyProbe.v1';export=b'AionReadOnlyProbeHook'
        if packet is None:packet=ProbePacket()
    elif protocol=='macro_cycle':
        protocol_version=2
        expected_size=832;magic=0x324f5243414d4941;prefix='AionRebuildMacroCycle-v2';message_name='AionRebuild.MacroCycle.v2';export=b'AionMacroCycleProbeHook'
        if packet is None or packet.mode not in range(9):raise ValueError('Pacote de ação interna inválido.')
    elif protocol=='movement':
        protocol_version=1
        expected_size=912;magic=0x31564f4d4e4f4941;prefix='AionRebuildMovement-v1';message_name='AionRebuild.Movement.v1';export=b'AionMovementProbeHook'
        if packet is None or packet.mode!=8 or packet.execute not in (0,1):
            raise ValueError('Pacote de movimento inválido.')
    elif protocol=='flight':
        protocol_version=1
        expected_size=856;magic=0x31594c464e4f4941;prefix='AionRebuildFlight-v1';message_name='AionRebuild.Flight.v1';export=b'AionFlightProbeHook'
        if packet is None or packet.mode!=9 or packet.execute not in (0,1,2):
            raise ValueError('Pacote de voo inválido.')
    elif protocol=='flight_movement':
        protocol_version=1
        expected_size=928;magic=0x31564d464e4f4941;prefix='AionRebuildFlightMovement-v1';message_name='AionRebuild.FlightMovement.v1';export=b'AionFlightMovementProbeHook'
        if packet is None or packet.mode!=8 or packet.execute not in (0,1,2,3):
            raise ValueError('Pacote de movimento aéreo inválido.')
    elif protocol=='flight_forward':
        protocol_version=1
        expected_size=864;magic=0x315746464e4f4941;prefix='AionRebuildFlightForward-v1';message_name='AionRebuild.FlightForward.v1';export=b'AionFlightForwardProbeHook'
        if packet is None or packet.mode!=10 or packet.execute not in (0,1) or not 50<=packet.hold_ms<=1500:
            raise ValueError('Pacote de W aereo invalido.')
    elif protocol=='axis':
        protocol_version=1
        expected_size=848;magic=0x3149584150494f41;prefix='AionRebuildAxis-v1';message_name='AionRebuild.Axis.v1';export=b'AionAxisProbeHook'
        if packet is None or packet.mode not in (10,11,13,16) or packet.execute not in (0,1) or packet.stop not in (0,1):
            raise ValueError('Pacote de eixo inválido.')
    elif protocol=='autorun':
        protocol_version=1
        expected_size=848;magic=0x314e5552544f4941;prefix='AionRebuildAutorun-v1';message_name='AionRebuild.Autorun.v1';export=b'AionAutorunProbeHook'
        if packet is None or packet.mode not in (12,14,15,17,18,19) or packet.execute not in (0,1):
            raise ValueError('Pacote de corrida automática inválido.')
    elif protocol=='auto_move':
        protocol_version=1
        expected_size=2440;magic=0x31564d414e4f4941;prefix='AionRebuildAutoMove-v1';message_name='AionRebuild.AutoMove.v1';export=b'AionAutoMoveProbeHook'
        if packet is None or packet.mode!=8 or packet.execute not in (0,1):
            raise ValueError('Pacote de deslocamento automático inválido.')
    else:raise ValueError('Protocolo nativo não reconhecido.')
    packet_type=type(packet)
    if completion_timeout is None:
        if protocol!='macro_cycle' or packet.mode!=4 or not getattr(process,'handle',None):
            raise ValueError('Espera persistente exige um período de macro e um processo identificado.')
    elif type(completion_timeout) not in (int,float) or not 0.1<=completion_timeout<=60:
        raise ValueError('Prazo de confirmação inválido.')
    if C.sizeof(packet_type)!=expected_size:raise RuntimeError('ABI do probe incompatível.')
    dll=Path(dll).resolve(strict=True)
    if not dll.is_file() or hashlib.sha256(dll.read_bytes()).hexdigest()!=digest:
        raise ValueError('DLL do probe não coincide com o artefato compilado.')
    api=PipeAPI();k=api.k;a=api.a;u=C.WinDLL('user32',use_last_error=True)
    k.CreateFileMappingW.argtypes=[W.HANDLE,C.POINTER(SecurityAttributes),W.DWORD,W.DWORD,W.DWORD,W.LPCWSTR];k.CreateFileMappingW.restype=W.HANDLE
    k.MapViewOfFile.argtypes=[W.HANDLE,W.DWORD,W.DWORD,W.DWORD,C.c_size_t];k.MapViewOfFile.restype=C.c_void_p
    k.UnmapViewOfFile.argtypes=[C.c_void_p];k.UnmapViewOfFile.restype=W.BOOL
    k.LoadLibraryExW.argtypes=[W.LPCWSTR,W.HANDLE,W.DWORD];k.LoadLibraryExW.restype=W.HMODULE
    k.FreeLibrary.argtypes=[W.HMODULE];k.FreeLibrary.restype=W.BOOL
    k.GetProcAddress.argtypes=[W.HMODULE,C.c_char_p];k.GetProcAddress.restype=C.c_void_p
    k.GetTickCount64.restype=C.c_uint64
    u.GetWindowThreadProcessId.argtypes=[W.HWND,C.POINTER(W.DWORD)];u.GetWindowThreadProcessId.restype=W.DWORD
    u.RegisterWindowMessageW.argtypes=[W.LPCWSTR];u.RegisterWindowMessageW.restype=W.UINT
    u.SetWindowsHookExW.argtypes=[C.c_int,C.c_void_p,W.HINSTANCE,W.DWORD];u.SetWindowsHookExW.restype=W.HANDLE
    u.UnhookWindowsHookEx.argtypes=[W.HANDLE];u.UnhookWindowsHookEx.restype=W.BOOL
    u.PostThreadMessageW.argtypes=[W.DWORD,W.UINT,W.WPARAM,W.LPARAM];u.PostThreadMessageW.restype=W.BOOL
    u.GetForegroundWindow.restype=W.HWND
    k.GetExitCodeProcess.argtypes=[W.HANDLE,C.POINTER(W.DWORD)];k.GetExitCodeProcess.restype=W.BOOL
    foreground_before=u.GetForegroundWindow()
    pid=W.DWORD();tid=u.GetWindowThreadProcessId(window,C.byref(pid))
    if not tid or pid.value!=process.pid:raise ValueError('Janela não pertence à sessão de jogo validada.')
    birth=int(process.session.split(':')[1]);nonce=secrets.randbits(64) or 1
    name=f'Local\\{prefix}-{process.pid:08x}-{nonce:016x}'
    descriptor=C.c_void_p();mapping=None;view=None;module=None;hook=None;cleanup={}
    sid=api.current_sid();sddl=f'D:P(A;;GA;;;SY)(A;;GA;;;{sid})'
    if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,C.byref(descriptor),None):api.error()
    try:
        attributes=SecurityAttributes(C.sizeof(SecurityAttributes),descriptor,False)
        C.set_last_error(0)
        mapping=k.CreateFileMappingW(W.HANDLE(-1),C.byref(attributes),4,0,expected_size,name)
        if not mapping:api.error()
        if C.get_last_error()==183:raise RuntimeError('Nome de probe já existe; não reutilizado.')
        view=k.MapViewOfFile(mapping,6,0,0,expected_size)
        if not view:api.error()
        packet.magic=magic;packet.version=protocol_version;packet.size=expected_size;packet.state=1
        packet.expected_pid=process.pid;packet.expected_tid=tid;packet.result=0;packet.nonce=nonce
        packet.expiry=k.GetTickCount64()+3000;packet.expected_birth=birth;packet.expected_base=process.base
        packet.expected_image_size=process.image_size
        C.memmove(view,C.byref(packet),C.sizeof(packet))
        module=k.LoadLibraryExW(str(dll),None,0x100|0x800)
        if not module:api.error()
        callback=k.GetProcAddress(module,export)
        if not callback:api.error()
        message=u.RegisterWindowMessageW(message_name)
        if not message:api.error()
        if require_background and u.GetForegroundWindow()==window:
            raise RuntimeError('Teste não enviado: o jogo voltou ao primeiro plano.')
        if abort_event is not None and abort_event.is_set():raise RuntimeError('Solicitação cancelada antes do envio.')
        hook=u.SetWindowsHookExW(3,callback,module,tid)
        if not hook:api.error()
        if not u.PostThreadMessageW(tid,message,nonce,0):api.error()
        if dispatch_event is not None:dispatch_event.set()
        shared=packet_type.from_address(view);deadline=None if completion_timeout is None else time.monotonic()+completion_timeout
        while shared.state!=3:
            if abort_event is not None and abort_event.is_set():raise RuntimeError('Espera encerrada; a renovação do período deve permanecer desligada.')
            if deadline is not None and time.monotonic()>=deadline:raise TimeoutError('O thread do jogo não confirmou o probe no prazo.')
            if completion_timeout is None:
                exit_code=W.DWORD()
                if not k.GetExitCodeProcess(process.handle,C.byref(exit_code)):api.error()
                if exit_code.value!=259:raise ProcessLookupError('O cliente do jogo encerrou.')
            time.sleep(0.01)
        result=packet_type.from_buffer_copy(C.string_at(view,expected_size))
        if (result.magic,result.version,result.size,result.nonce)!=(magic,protocol_version,expected_size,nonce):
            raise RuntimeError('Resposta de probe não corresponde à solicitação.')
        report={'probe_result':result.result,'game_thread_roundtrip_verified':result.result==0,
            'observed_pid':result.observed_pid,'observed_tid':result.observed_tid,
            'birth_matches':result.observed_birth==birth,'image_base_matches':result.observed_base==process.base,
            'image_size_matches':result.observed_image_size==process.image_size,
            'game_functions_called':bool(getattr(result,'calls',0)),'keyboard_or_mouse_events_sent':False,'requests_foreground':False,
            'background_commands_implemented':False,'cleanup':cleanup}
        if protocol=='flight_movement':
            report.update(flight_stream_acknowledged=result.execute in (2,3) and result.result==0,
                          flight_stream_stopped=result.execute==3 and result.result==0)
        foreground_after=u.GetForegroundWindow()
        report.update(game_window_was_foreground=foreground_before==window,
            game_window_is_foreground=foreground_after==window,foreground_window_unchanged=foreground_before==foreground_after)
        if protocol=='macro_cycle':
            report.update(native_action_callback_calls=result.calls,native_macro_callback_calls=result.calls if result.mode not in (2,3,8) else 0,
                macro_cycle_completed=result.mode==1 and result.result==0 and result.calls==3,
                failure_stage=result.failure_stage,
                difference_count=result.diff_count,differences_truncated=result.diff_count>64,
                differences=[{'offset':item.offset,'before':item.before,'active':item.active,'after':item.after,
                    'restored':item.before==item.after} for item in result.differences[:min(result.diff_count,64)]],
                macro_state_validated=False)
            report.update(next_target_dispatched=result.mode==3 and result.result==0 and result.calls==1,
                collect_dispatched=result.mode==8 and result.result==0 and result.calls==1)
            trace=[bool(value) if result.macro_state_mask&(1<<index) and value<=1 else None
                   for index,value in enumerate((result.macro_before,result.macro_active,result.macro_after))]
            transition=result.mode in (1,4) and result.result==0 and result.calls==3 and result.macro_state_mask==7 and trace==[False,True,False]
            report.update(macro_state_trace=trace,macro_state_snapshot_mask=result.macro_state_mask,
                          macro_transition_observed=transition,macro_state_validated=transition)
            report.update(macro_lease_completed=result.mode==4 and transition,
                          native_timer_release_confirmed=result.mode==4 and transition,
                          native_lease_duration_ms=500 if result.mode==4 else None,
                          native_module_resident_until_game_exit=result.mode==4 and result.calls>0,
                          background_action_probe_succeeded=result.result==0 and result.calls>0 and foreground_before!=window and foreground_after!=window)
            report.update(macro_lease_renewed=result.mode==5 and result.result==0 and trace==[True,None,True],
                          macro_stop_confirmed=result.mode in (6,7) and result.result==0 and trace[2] is False)
        if protocol in ('movement','flight_movement'):
            report.update(movement_callbacks=result.calls,
                          movement_validated=result.result==0,
                          movement_pulse_completed=result.execute==1 and result.result==0 and result.calls>0,
                          movement_duration_ms=result.duration_ms,
                          native_module_resident_until_game_exit=result.execute in (1,2) and result.calls>0)
            report['movement_dimension']='3d_flying' if protocol=='flight_movement' else 'planar'
        if protocol=='flight_forward':
            report.update(flight_forward_validated=result.result==0,flight_forward_stage=result.stage,
                flight_forward_released=result.execute==1 and result.stage==3 and result.result==0,
                flight_forward_completed=result.execute==1 and result.stage==3 and result.result==0 and result.calls>=3,
                flight_forward_calls=result.calls,failure_stage=result.failure_stage)
        if protocol=='axis':
            report.update(axis_binding_validated=result.result==0,axis_value=result.value,axis_callback_calls=result.calls,
                axis_stop_phase=result.stop==1,axis_dispatched=result.execute==1 and result.result==0 and result.calls==1,failure_stage=result.failure_stage)
        if protocol=='autorun':
            report.update(autorun_validated=result.result==0,autorun_stage=result.stage,
                autorun_dispatched=result.execute==1 and result.result==0 and result.calls==1,
                failure_stage=result.failure_stage)
        if protocol=='auto_move':
            report.update(auto_move_validated=result.result==0,auto_move_stage=result.stage,auto_move_mode_before=result.mode_before,
                route_point_count=result.point_count,flag_before=result.flag_before,flag_after=result.flag_after,
                route_points=[tuple(result.points[i]) for i in range(min(result.point_count,64))] if result.plan else [],
                auto_move_dispatched=result.execute==1 and result.result==0 and result.calls>=1,
                failure_stage=result.failure_stage)
        if protocol=='flight':
            report.update(flight_binding_validated=result.result==0,
                flight_action_dispatched=result.execute in (1,2) and result.result==0 and result.calls==1,
                flight_callback_calls=result.calls,movement_mode_before=result.movement_mode_before,
                flight_transition_verified=False,failure_stage=result.failure_stage)
        return report
    finally:
        if hook:
            cleanup['hook_removed']=bool(u.UnhookWindowsHookEx(hook))
            if not cleanup['hook_removed'] and C.get_last_error()==1404 and getattr(process,'handle',None):
                exit_code=W.DWORD()
                if k.GetExitCodeProcess(process.handle,C.byref(exit_code)) and exit_code.value!=259:
                    cleanup['hook_removed']=True;cleanup['hook_removed_by_target_exit']=True
        if module:cleanup['host_module_released']=bool(k.FreeLibrary(module))
        if view:cleanup['view_unmapped']=bool(k.UnmapViewOfFile(view))
        if mapping:cleanup['mapping_handle_closed']=bool(k.CloseHandle(mapping))
        if descriptor:cleanup['descriptor_freed']=not bool(k.LocalFree(descriptor))
        failed=[name for name,ok in cleanup.items() if not ok]
        if failed:raise RuntimeError('Limpeza do probe não confirmada: '+', '.join(failed))
