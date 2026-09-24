"""Konsistente, read-only-nahe Statuskopien für die API."""
from copy import deepcopy
from types import SimpleNamespace


def _copy_value(value):
    try:
        return deepcopy(value)
    except Exception:
        return value


def _copy_substate(source):
    if source is None:
        return SimpleNamespace()
    values = {
        key: _copy_value(value)
        for key, value in vars(source).items()
        if key not in {"gpio_lock"}
    }
    return SimpleNamespace(**values)


def build_status_snapshot(state):
    """Erzeugt einen konsistenten Snapshot ohne GPIO-/Session-Referenzen."""
    snapshot = SimpleNamespace(_is_status_snapshot=True)
    for name in ("sensors", "solar", "control", "stats"):
        setattr(snapshot, name, _copy_substate(getattr(state, name, None)))
    for name in ("config", "priority_config"):
        setattr(snapshot, name, _copy_value(getattr(state, name, None)))

    # Root-Felder, die von /status und der WebApp benötigt werden.
    root_fields = (
        "local_tz", "bademodus_aktiv", "urlaubsmodus_aktiv",
        "sicherheits_temp", "verdampfertemperatur",
        "sommer_modus_aktiv", "sommer_modus_zaehler",
        "sommer_modus_offset_c", "sommer_modus_tage_ueber",
        "sommer_modus_benoetigte", "legionellen_aktiv",
        "legionellen_last_done", "legionellen_planned_day",
        "legionellen_planned_time", "legionellen_planned_reason",
        "legionellen_target_temp_c", "legionellen_max_duration_hours",
        "solar_stale", "forecast_stale", "forecast_age_s", "learning_engine",
        "last_api_call", "last_forecast_update", "last_forecast_attempt",
    )
    for name in root_fields:
        if hasattr(state, name):
            setattr(snapshot, name, _copy_value(getattr(state, name)))
    return snapshot
