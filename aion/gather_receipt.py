"""Independent reconstruction of collection outcome checks.

Evidence: core.gather constants in activity_binary_inventory.json reference
interaction ownership, native progress and inventory gains. This implements
those requirements independently; it is not decompiled original source.
"""
from dataclasses import dataclass
from enum import Enum
import math


class GatherOutcome(str, Enum):
    WAITING = 'waiting'
    SUCCESS = 'success'
    UNCONFIRMED = 'unconfirmed'
    WRONG_SESSION = 'wrong_session'
    OTHER_INTERACTION = 'other_interaction'
    FAILURE = 'failure'


@dataclass(frozen=True)
class GatherEvidence:
    session: str
    source_identity: str
    observed_at: float
    inventory: tuple[tuple[int, int], ...]
    inventory_stable: bool
    interaction_owner: str | None
    progress_visible: bool
    interaction_active: bool
    interaction_identity: tuple[int,int] | None = None
    result_kind: str | None = None


class GatherReceipt:
    """Tracks a single submitted extraction without automatically retrying it.

    Inventory contains aggregate item-definition totals, including stacks.
    The adapter must attribute the interaction to the same resource identity;
    a generic visible progress panel is insufficient.
    """
    def __init__(self, before: GatherEvidence):
        self._validate(before)
        if (not before.session or not before.source_identity
                or not math.isfinite(before.observed_at)
                or before.inventory_stable is not True
                or before.interaction_active or before.progress_visible
                or before.interaction_owner is not None or before.result_kind is not None):
            raise ValueError('Coleta exige sessão, recurso e inventário estável sem interação ativa.')
        self.before = before
        self.baseline = self._items(before.inventory)
        self.owned_seen = False
        self.progress_seen = False
        self.last_observed_at = before.observed_at
        self.final = None
        self.interaction_identity = None

    @staticmethod
    def _validate(evidence):
        if (not isinstance(evidence, GatherEvidence)
                or type(evidence.session) is not str or not evidence.session
                or type(evidence.source_identity) is not str or not evidence.source_identity
                or type(evidence.observed_at) not in (int, float)
                or not math.isfinite(evidence.observed_at)
                or any(type(value) is not bool for value in (
                    evidence.inventory_stable, evidence.progress_visible, evidence.interaction_active))
                or evidence.interaction_owner is not None and type(evidence.interaction_owner) is not str):
            raise ValueError('Evidência de coleta inválida ou incompleta.')
        if evidence.result_kind not in (None,'success','failure'):
            raise ValueError('Resultado nativo de coleta inválido.')
        identity=evidence.interaction_identity
        if identity is not None and (not isinstance(identity,tuple) or len(identity)!=2
                or any(type(value) is not int for value in identity)
                or identity[0]<0 or not 0<identity[1]<=0xffffffff):
            raise ValueError('Identidade da interação de coleta inválida.')

    @staticmethod
    def _items(items):
        result = {}
        for item, count in items:
            if (type(item) is not int or item <= 0 or type(count) is not int
                    or count < 0 or item in result):
                raise ValueError('Inventário agregado inválido.')
            result[item] = count
        return result

    def observe(self, current: GatherEvidence, *, expired=False):
        if self.final is not None:
            return self.final
        self._validate(current)
        if current.session != self.before.session:
            self.final = GatherOutcome.WRONG_SESSION
            return self.final
        if not math.isfinite(current.observed_at) or current.observed_at <= self.last_observed_at:
            if expired:
                self.final = GatherOutcome.UNCONFIRMED
                return self.final
            return GatherOutcome.WAITING
        self.last_observed_at = current.observed_at
        if (current.source_identity != self.before.source_identity
                or current.interaction_owner not in (None, self.before.source_identity)):
            self.final = GatherOutcome.OTHER_INTERACTION
            return self.final
        owned = current.interaction_owner == self.before.source_identity
        if owned and current.interaction_active:
            identity=current.interaction_identity
            if self.interaction_identity is not None and identity!=self.interaction_identity:
                self.final=GatherOutcome.OTHER_INTERACTION
                return self.final
            if identity is not None:self.interaction_identity=identity
        self.owned_seen |= owned and current.interaction_active
        self.progress_seen |= owned and current.progress_visible and current.interaction_active
        if current.result_kind=='failure' and self.owned_seen:
            self.final=GatherOutcome.FAILURE
            return self.final
        if current.interaction_active:
            if expired:
                self.final = GatherOutcome.UNCONFIRMED
                return self.final
            return GatherOutcome.WAITING
        if current.inventory_stable is not True:
            if expired:
                self.final = GatherOutcome.UNCONFIRMED
                return self.final
            return GatherOutcome.WAITING
        after = self._items(current.inventory)
        gained = any(count > self.baseline.get(item, 0) for item, count in after.items())
        if self.owned_seen and self.progress_seen and gained:
            self.final = GatherOutcome.SUCCESS
        elif expired:
            self.final = GatherOutcome.UNCONFIRMED
        return self.final or GatherOutcome.WAITING
