"""Shared bounded metadata recovery for diagnostics and the read-only worker."""
from dataclasses import replace
from .metadata_templates import recover_templates,MetadataRecoveryError
from .name_pool import inspect_metadata


def recover_observation(process,templates,*,wide=False):
    if type(wide) is not bool:raise ValueError('Modo de busca inválido.')
    if wide:
        templates=tuple(replace(t,radius=4194304) if t.radius else t for t in templates)
    budget=33554432 if wide else 2097152
    try:
        result=recover_templates(lambda rva,size:process.read(process.base+rva,size),
                                process.image_size,templates,max_bytes=budget)
        found=result['roots']
    except MetadataRecoveryError as exc:
        evidence=exc.partial or {}
        found=evidence.get('diagnostic_read_roots') or evidence.get('independent_read_roots')
        result={'profile_error':str(exc),'partial_evidence':evidence,
                'profile_promoted':False,'gameplay_compatibility_validated':False}
        if not found:
            return None,{**result,'metadata_validated':False,'attacks':False,'wide_search':wide}
    observation=inspect_metadata(process,found)
    return found,{**result,'metadata_observation':observation,'attacks':False,'wide_search':wide}
