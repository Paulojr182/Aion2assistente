"""Exact integer interpretation recovered from health_mana constants.

This module does not discover an actor's resource container or prove hostility.
"""
from dataclasses import dataclass


class ResourceValueError(ValueError):
    pass


@dataclass(frozen=True)
class ResourcePair:
    current: int
    maximum: int

    def to_dict(self):
        # Decimal strings preserve HP beyond JavaScript's integer precision.
        return {'current_decimal':str(self.current),'maximum_decimal':str(self.maximum)}


def decode_pair(values,current_key,maximum_key,width):
    decoded=[]
    for key in (current_key,maximum_key):
        if key not in values:raise ResourceValueError('Recurso obrigatório ausente.')
        raw=values[key]
        if type(raw) is not int or not 0<=raw<1<<64:
            raise ResourceValueError('Recurso não é um inteiro unsigned de 64 bits.')
        value=raw&((1<<width)-1)
        if value&(1<<(width-1)):raise ResourceValueError('Recurso assinado negativo.')
        decoded.append(value)
    return ResourcePair(*decoded)


def decode_health(values):
    return decode_pair(values,1,9,64)


def decode_mana(values):
    return decode_pair(values,2,10,32)
