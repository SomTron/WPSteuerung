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


def _learning_summary(source):
    """Kompakte, API-sichere Lerninformationen statt Deepcopy des Engines."""
    if source is None:
        return None
    try:
        info = _copy_value(source.get_info())
    except Exception:
        return {}
    if not isinstance(info, dict):
        return {}
    return {
        key: info.get(key)
        for key in (
            "heat_rates", "learned_target_hour", "target_hour_samples",
            "total_cycles", "total_usage_events", "learned_evening_window",
            "learned_morning_target_hour", "morning_target_hour_samples",
            "learned_morning_window", "komfort_verletzungen_7d",
            "komfort_verletzungen_1d", "forecast_ratio",
            "forecast_ratio_samples", "forecast_hourly_deviations",
            "quellen", "surplus_stunden", "surplus_profil", "letzte_zapfung",
        )
        if key in info
    }


def build_status_snapshot(state):
    """Erzeugt einen konsistenten Snapshot ohne GPIO-/Session-Referenzen."""
    snapshot = SimpleNamespace(
        _is_status_snapshot=True,
        _is_status_snapshot_created_at=getattr(state, "last_status_snapshot_at", None),
    )
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
        "solar_stale", "forecast_stale", "forecast_age_s",
        "loop_heartbeat", "last_control_success", "last_sensor_success",
        "last_status_snapshot_at", "last_data_update_ok",
        "consecutive_control_errors",
        "energy_source", "energy_source_detail", "clock",
        "last_api_call", "last_forecast_update", "last_forecast_attempt",
        "last_state_write_ok", "last_state_write_error",
    )
    setattr(snapshot, "learning_engine_summary", _learning_summary(
        getattr(state, "learning_engine", None)
    ))
    for name in root_fields:
        if name == "learning_engine_summary":
            continue
        if hasattr(state, name):
            setattr(snapshot, name, _copy_value(getattr(state, name)))
    return snapshot
