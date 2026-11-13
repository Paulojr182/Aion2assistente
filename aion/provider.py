"""Contract for a future independently implemented live game provider.

Merely finding the game window does not satisfy this contract. Providers must
return current native observations and must acknowledge actual command results.
No implementation from the old executable is loaded by this project.
"""
from typing import Protocol
from dataclasses import dataclass
from .core import Observation, Command


class IntegrationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderCapabilities:
    observation: bool = False
    selection: bool = False
    macro_control: bool = False
    macro_state: bool = False
    skills: bool = False
    background_commands: bool = False


class GameProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities:
        ...

    def observe(self) -> Observation:
        """Read current session, entity identities, selection and skill state."""
        ...

    def macro_state(self, *, session: str | None) -> bool | None:
        """Return a confirmed macro state, or None when unknown."""
        ...

    def execute(self, command: Command, *, session: str, macro_key: str,
                expected_target: tuple[int,int] | None) -> bool:
        """Execute against exactly this session through a verified transport.

        set_macro is idempotent desired-state semantics. If only a toggle key
        is available, the adapter must establish its initial state and track
        confirmed transitions; uncertain delivery must NOT be acknowledged.
        select_target must include actor identity to reject recycled entity IDs.
        Before starting a macro or casting, recheck expected_target, readiness
        and session immediately before delivery. A physical-key transport also
        requires foreground; a background transport must never take focus or
        send keyboard/mouse events to the desktop. False means not confirmed.
        """
        ...

    def close(self) -> None:
        ...


def connect_live() -> GameProvider:
    raise IntegrationUnavailable(
        'As leituras nativas estão disponíveis no diagnóstico, mas o provedor '
        'de comandos e a confirmação do estado do macro ainda não estão integrados.')
