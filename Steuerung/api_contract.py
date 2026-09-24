"""Gemeinsame API-/Status-Verträge und Versionierung."""

from typing import Any, Iterable


API_CONTRACT_VERSION = "1.0.0"
STATUS_SCHEMA_VERSION = 1


def with_contract_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Versieht eine Statusantwort ohne bestehende Schlüssel zu verändern."""
    result = dict(payload)
    result["_contract"] = {
        "api_version": API_CONTRACT_VERSION,
        "status_schema_version": STATUS_SCHEMA_VERSION,
    }
    return result


def required_status_keys() -> frozenset[str]:
    """Die von WebApp und Android benötigten Top-Level-Schlüssel."""
    return frozenset({
        "temperatures", "compressor", "setpoints", "mode", "energy",
        "system", "priority", "regel_ergebnisse", "status_indikatoren",
    })


def validate_status_shape(payload: dict[str, Any]) -> None:
    """Prüft den Antwortvertrag beim Start/Tests und wirft bei API-Regressions."""
    if not isinstance(payload, dict):
        raise TypeError("Statusantwort muss ein dict sein")
    fehlend = required_status_keys() - payload.keys()
    if fehlend:
        raise ValueError(f"Statusantwort fehlen: {sorted(fehlend)}")
    contract = payload.get("_contract")
    if not isinstance(contract, dict):
        raise ValueError("Statusantwort enthält keinen _contract-Marker")
    if contract.get("status_schema_version") != STATUS_SCHEMA_VERSION:
        raise ValueError("Status-Schemaversion passt nicht zum API-Vertrag")


def stable_keys(values: Iterable[str]) -> tuple[str, ...]:
    """Für reproduzierbare Diagnosemeldungen."""
    return tuple(sorted(set(values)))
