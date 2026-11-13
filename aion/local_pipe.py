"""Local Windows named pipe restricted to the current Windows user and SYSTEM."""
import ctypes as C
from ctypes import wintypes as W
import re,time

INVALID=C.c_void_p(-1).value


class SecurityAttributes(C.Structure):
    _fields_=[('length',W.DWORD),('descriptor',C.c_void_p),('inherit',W.BOOL)]


def path(name,kind='read_only'):
    if kind not in ('read_only','combat'):raise ValueError('Tipo de canal inválido.')
    prefix='AionRebuildReadOnly' if kind=='read_only' else 'AionRebuildCombat'
    if type(name) is not str or not re.fullmatch(prefix+'-[0-9a-f]{32}',name):
        raise ValueError('Nome de canal inválido.')
    return '\\\\.\\pipe\\'+name


class PipeAPI:
    def __init__(self,kind='read_only'):
        if kind not in ('read_only','combat'):raise ValueError('Tipo de canal inválido.')
        self.kind=kind
        self.k=C.WinDLL('kernel32',use_last_error=True)
        self.a=C.WinDLL('advapi32',use_last_error=True)
        k=self.k;a=self.a
        k.CloseHandle.argtypes=[W.HANDLE];k.CloseHandle.restype=W.BOOL
        k.LocalFree.argtypes=[C.c_void_p];k.LocalFree.restype=C.c_void_p
        k.GetCurrentProcess.restype=W.HANDLE
        a.OpenProcessToken.argtypes=[W.HANDLE,W.DWORD,C.POINTER(W.HANDLE)];a.OpenProcessToken.restype=W.BOOL
        a.GetTokenInformation.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.POINTER(W.DWORD)]
        a.GetTokenInformation.restype=W.BOOL
        a.ConvertSidToStringSidW.argtypes=[C.c_void_p,C.POINTER(C.c_void_p)];a.ConvertSidToStringSidW.restype=W.BOOL
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[W.LPCWSTR,W.DWORD,C.POINTER(C.c_void_p),C.POINTER(W.DWORD)]
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype=W.BOOL
        k.CreateNamedPipeW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,C.POINTER(SecurityAttributes)]
        k.CreateNamedPipeW.restype=W.HANDLE
        k.ConnectNamedPipe.argtypes=[W.HANDLE,C.c_void_p];k.ConnectNamedPipe.restype=W.BOOL
        k.DisconnectNamedPipe.argtypes=[W.HANDLE];k.DisconnectNamedPipe.restype=W.BOOL
        k.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
        k.CreateFileW.restype=W.HANDLE
        k.WaitNamedPipeW.argtypes=[W.LPCWSTR,W.DWORD];k.WaitNamedPipeW.restype=W.BOOL
        k.SetNamedPipeHandleState.argtypes=[W.HANDLE,C.POINTER(W.DWORD),C.c_void_p,C.c_void_p]
        k.SetNamedPipeHandleState.restype=W.BOOL
        k.ReadFile.argtypes=[W.HANDLE,C.c_void_p,W.DWORD,C.POINTER(W.DWORD),C.c_void_p];k.ReadFile.restype=W.BOOL
        k.WriteFile.argtypes=[W.HANDLE,C.c_void_p,W.DWORD,C.POINTER(W.DWORD),C.c_void_p];k.WriteFile.restype=W.BOOL

    def error(self):raise C.WinError(C.get_last_error())

    def current_sid(self):
        token=W.HANDLE()
        if not self.a.OpenProcessToken(self.k.GetCurrentProcess(),8,C.byref(token)):self.error()
        text=C.c_void_p()
        try:
            needed=W.DWORD();self.a.GetTokenInformation(token,1,None,0,C.byref(needed))
            if not 1<=needed.value<=65536:raise ValueError('Token de usuário inválido.')
            info=C.create_string_buffer(needed.value)
            if not self.a.GetTokenInformation(token,1,info,len(info),C.byref(needed)):self.error()
            sid=C.cast(info,C.POINTER(C.c_void_p))[0]
            if not self.a.ConvertSidToStringSidW(sid,C.byref(text)):self.error()
            return C.wstring_at(text)
        finally:
            if text:self.k.LocalFree(text)
            self.k.CloseHandle(token)

    def server(self,name):
        sid=self.current_sid();descriptor=C.c_void_p()
        # Explicit same-user access; do not alter integrity labels, tokens,
        # process privileges or system UAC policy.
        sddl=f'D:P(A;;GA;;;SY)(A;;GA;;;{sid})'
        if not self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,C.byref(descriptor),None):self.error()
        try:
            attributes=SecurityAttributes(C.sizeof(SecurityAttributes),descriptor,False)
            handle=self.k.CreateNamedPipeW(path(name,self.kind),3|0x00080000,0xF,1,65536,2048,1000,C.byref(attributes))
            if handle==INVALID:self.error()
            return handle
        finally:self.k.LocalFree(descriptor)

    def connect_client(self,name):
        full=path(name,self.kind)
        if not self.k.WaitNamedPipeW(full,5000):self.error()
        handle=self.k.CreateFileW(full,0xC0000000,0,None,3,0,None)
        if handle==INVALID:self.error()
        mode=W.DWORD(3)
        if not self.k.SetNamedPipeHandleState(handle,C.byref(mode),None,None):
            error=C.get_last_error();self.k.CloseHandle(handle);raise C.WinError(error)
        return handle

    def accept(self,handle,timeout=0.5):
        deadline=time.monotonic()+timeout
        while True:
            if self.k.ConnectNamedPipe(handle,None):return True
            error=C.get_last_error()
            if error==535:return True
            if error==232:self.k.DisconnectNamedPipe(handle)
            elif error!=536:raise C.WinError(error)
            if time.monotonic()>=deadline:return False
            time.sleep(0.02)

    def receive(self,handle,limit,timeout=10):
        data=C.create_string_buffer(limit);count=W.DWORD();deadline=time.monotonic()+timeout
        while True:
            if self.k.ReadFile(handle,data,limit,C.byref(count),None):return data.raw[:count.value]
            error=C.get_last_error()
            if error not in (232,536):raise C.WinError(error)
            if time.monotonic()>=deadline:raise TimeoutError('Tempo de leitura no canal local esgotado.')
            time.sleep(0.02)

    def send(self,handle,data):
        if type(data) is not bytes or len(data)>65536:raise ValueError('Mensagem fora do limite.')
        count=W.DWORD();buffer=C.create_string_buffer(data)
        if not self.k.WriteFile(handle,buffer,len(data),C.byref(count),None):self.error()
        if count.value!=len(data):raise OSError('Envio parcial no canal local.')
