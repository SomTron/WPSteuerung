"""Zentrale Laufzeitverträge für sichere Fehlerdiagnosen."""

from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any


class RuntimeContractError(RuntimeError):
    """Der Start darf bei einem verletzten Laufzeitvertrag nicht fortfahren."""


@dataclass(frozen=True)
class RuntimeContract:
    """Erwartete Kernobjekte und minimale Initialwerte des laufenden Systems."""

    state_type: type
    required_state_fields: tuple[str, ...]
    required_control_fields: tuple[str, ...]
    required_sensor_fields: tuple[str, ...]


DEFAULT_CONTRACT = RuntimeContract(
    state_type=object,
    required_state_fields=(
        "config", "priority_config", "local_tz", "sensors", "solar", "control", "stats",
    ),
    required_control_fields=("kompressor_ein", "alle_ergebnisse", "blocking_reason"),
    required_sensor_fields=("t_oben", "t_unten", "t_mittig", "t_verd"),
)


def _has_fields(value: Any, fields: tuple[str, ...]) -> list[str]:
    return [field for field in fields if not hasattr(value, field)]


def validate_runtime_contract(
    state: Any,
    contract: RuntimeContract = DEFAULT_CONTRACT,
) -> None:
    """Prüft die für den Main-Loop zwingenden Verträge fail-fast."""
    if state is None:
        raise RuntimeContractError("Runtime-State ist None")
    missing = _has_fields(state, contract.required_state_fields)
    if missing:
        raise RuntimeContractError(f"Runtime-State-Felder fehlen: {', '.join(missing)}")
    for substate_name, fields in (
        ("control", contract.required_control_fields),
        ("sensors", contract.required_sensor_fields),
    ):
        substate = getattr(state, substate_name, None)
        missing = _has_fields(substate, fields)
        if missing:
            raise RuntimeContractError(
                f"Runtime-State {substate_name}-Felder fehlen: {', '.join(missing)}"
            )


def validate_startup_dependencies() -> None:
    """Stellt sicher, dass die sicherheitskritischen Module importierbar sind."""
    required = ("asyncio", "logging", "datetime", "json")
    missing = [name for name in required if find_spec(name) is None]
    if missing:
        raise RuntimeContractError(f"Laufzeit-Bibliotheken fehlen: {', '.join(missing)}")
