"""Read-only Windows process access for independently reconstructed observers.

No process-memory writes, injected DLLs, remote threads or packet capture.
"""
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import os
import struct
import threading


class ProcessReadError(OSError):
    pass


def compare_image_headers(memory, disk):
    """Compare PE headers; raw-file pointers of empty sections have no live data.

    Accept only their observed transition to zero in the loaded image. No code,
    identity, section RVA, virtual size or nonempty raw section is normalized.
    """
    if len(disk)<64 or len(memory)<64 or disk[:2]!=b'MZ' or memory[:2]!=b'MZ':
        raise ProcessReadError('Assinatura DOS inválida.')
    pe=struct.unpack_from('<I',disk,60)[0]
    if pe<64 or pe>min(len(memory),len(disk))-88 or disk[pe:pe+4]!=b'PE\0\0':
        raise ProcessReadError('Cabeçalho PE inválido.')
    size=struct.unpack_from('<I',disk,pe+24+60)[0]
    sections=struct.unpack_from('<H',disk,pe+6)[0]
    optional_size=struct.unpack_from('<H',disk,pe+20)[0]
    table=pe+24+optional_size
    if not 0<size<=min(len(memory),len(disk)) or sections>96 or table+40*sections>size:
        raise ProcessReadError('Limites do cabeçalho PE inválidos.')
    normalized=bytearray(memory[:size]);fields=[]
    for i in range(sections):
        section=table+40*i
        raw_size=struct.unpack_from('<I',disk,section+16)[0]
        if raw_size==0 and memory[section+20:section+24]==bytes(4):
            if disk[section+20:section+24]!=bytes(4):
                normalized[section+20:section+24]=disk[section+20:section+24]
                fields.append(f'Section[{i}].PointerToRawData(empty)')
    if bytes(normalized)!=disk[:size]:
        differences=[i for i in range(size) if normalized[i]!=disk[i]]
        raise ProcessReadError(f'Cabeçalhos incompatíveis: {len(differences)} bytes; offsets {differences[:12]}.')
    return fields


class ReadOnlyProcess:
    def __init__(self, pid, expected_image, *, max_bytes=2*1024*1024):
        if os.name != 'nt':
            raise ProcessReadError('Esta implementação requer Windows.')
        if type(pid) is not int or pid <= 0 or type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError('PID ou orçamento de leitura inválido.')
        self.pid = pid
        self.handle = None
        self.limit = max_bytes
        self.used = 0
        self.lock = threading.RLock()
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.psapi = C.WinDLL('psapi', use_last_error=True)
        k = self.kernel
        k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        k.OpenProcess.restype = W.HANDLE
        k.CloseHandle.argtypes = [W.HANDLE]
        k.CloseHandle.restype = W.BOOL
        k.ReadProcessMemory.argtypes = [W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)]
        k.ReadProcessMemory.restype = W.BOOL
        k.QueryFullProcessImageNameW.argtypes = [W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)]
        k.QueryFullProcessImageNameW.restype = W.BOOL
        k.GetProcessId.argtypes = [W.HANDLE]
        k.GetProcessId.restype = W.DWORD
        k.GetProcessTimes.argtypes = [W.HANDLE]+[C.POINTER(W.FILETIME)]*4
        k.GetProcessTimes.restype = W.BOOL
        self.psapi.EnumProcessModulesEx.argtypes = [W.HANDLE,C.POINTER(C.c_void_p),W.DWORD,C.POINTER(W.DWORD),W.DWORD]
        self.psapi.EnumProcessModulesEx.restype = W.BOOL
        self.handle = k.OpenProcess(0x0400 | 0x0010, False, pid)
        if not self.handle:
            raise ProcessReadError(C.get_last_error(), 'Não foi possível abrir o processo para leitura.')
        try:
            path = C.create_unicode_buffer(32768)
            length = W.DWORD(len(path))
            if not k.QueryFullProcessImageNameW(self.handle, 0, path, C.byref(length)):
                self._error('QueryFullProcessImageNameW')
            self.image_path = Path(path.value).resolve()
            if str(self.image_path).casefold() != str(Path(expected_image).resolve()).casefold():
                raise ProcessReadError('O processo não corresponde ao executável esperado.')
            created, exited, kernel_time, user_time = (W.FILETIME() for _ in range(4))
            if not k.GetProcessTimes(self.handle, C.byref(created), C.byref(exited),
                                     C.byref(kernel_time), C.byref(user_time)):
                self._error('GetProcessTimes')
            self.birth = created.dwLowDateTime | (created.dwHighDateTime << 32)
            modules = (C.c_void_p * 512)()
            needed = W.DWORD()
            if not self.psapi.EnumProcessModulesEx(self.handle, modules, C.sizeof(modules),C.byref(needed),3):
                self._error('EnumProcessModulesEx')
            if needed.value < C.sizeof(C.c_void_p) or needed.value > C.sizeof(modules) or not modules[0]:
                raise ProcessReadError('Lista de módulos ausente ou maior que o limite.')
            self.base = modules[0]
            header = self.read(self.base, 4096)
            disk = self.image_path.open('rb')
            with disk:
                expected = disk.read(4096)
            if header[:2] != b'MZ' or expected[:2] != b'MZ':
                raise ProcessReadError('O cabeçalho em memória não corresponde ao arquivo instalado.')
            offset = struct.unpack_from('<I', header, 0x3c)[0]
            if offset > len(header)-88 or header[offset:offset+4] != b'PE\0\0':
                raise ProcessReadError('Cabeçalho PE inválido.')
            if struct.unpack_from('<H',header,offset+4)[0] != 0x8664:
                raise ProcessReadError('O processo não é um executável x64.')
            self.normalized_fields=compare_image_headers(header,expected)
            self.image_size = struct.unpack_from('<I',header,offset+24+56)[0]
            self.session = f'{self.pid}:{self.birth}:{self.base:x}'
        except BaseException:
            self.close()
            raise

    def _error(self, operation):
        raise ProcessReadError(C.get_last_error(), operation+' falhou.')

    def read(self, address, size):
        with self.lock:
            if not self.handle or self.kernel.GetProcessId(self.handle) != self.pid:
                raise ProcessReadError('O processo não está mais disponível.')
            if (type(address) is not int or address < 0x10000 or address > 0x7fffffffffff
                    or type(size) is not int or size <= 0 or address+size > 0x800000000000):
                raise ValueError('Endereço ou tamanho de leitura inválido.')
            if size > self.limit-self.used:
                raise ProcessReadError('Orçamento de leitura esgotado.')
            self.used += size
            data = C.create_string_buffer(size)
            count = C.c_size_t()
            if not self.kernel.ReadProcessMemory(self.handle,address,data,size,C.byref(count)):
                self._error('ReadProcessMemory')
            if count.value != size:
                raise ProcessReadError('Leitura parcial; dados rejeitados.')
            return data.raw

    def close(self):
        with self.lock:
            if self.handle:
                handle, self.handle = self.handle, None
                self.kernel.CloseHandle(handle)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
