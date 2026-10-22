"""Alarmierung bei Ausfall und Erholung der Solax-Live-Daten.

BUGFIX (22.10.2026): Aus main.py herausgezogen. Die beiden Funktionen
bilden eine geschlossene Gruppe - sie brauchen nur `state`, den
Telegram-Versand und zwei Konstanten. In main.py (2758 Zeilen) waren
sie zwischen Hardware-Setup und Perioden-Task eingebettet, ohne
eigenstaendigen Bezug.

Fachlich unveraendert - der Block wurde 1:1 verschoben. Bewusst KEIN
eigener Alarmweg: der besteht ueber `blocking_reason` dort, wo der
Grund mit Dutzenden anderen Sperrgruenden konkurriert (Mindestpause,
Start-Antizipation, ...) - das fuehrt zu Alarm-Wechseln und Spam.
Stattdessen wird der bestehende Drosselmechanismus genutzt.
"""
import logging

import control_logic
from constants import SOLAR_DATA_STALE_THRESHOLD_MIN, STALE_ALARM_MINUTEN

# BUGFIX (22.10.2026): Die Drosselung wird ueber das Modul-Attribut
# aufgeloest, damit ein Test sie weiterhin patchen kann:
#   monkeypatch.setattr(main, "check_log_throttle", ...)
# Ein "from ... import check_log_throttle" wuerde eine EIGENE Referenz
# im neuen Modul binden - der Patch in main kaeme dann nicht an.
# Deshalb: der Zugriff erfolgt ueber dieses Modul.
import logic_utils  # noqa: E402


def check_log_throttle(state, key, interval_minutes=0.0):
    """Weiterleitung an die zentrale Drosselung (patchbar)."""
    return logic_utils.check_log_throttle(
        state, key, interval_minutes=interval_minutes
    )


async def _melde_solar_ausfall(session, state, alter_min):
    """Alarmiert ueber den Ausfall der Solax-Live-Daten.

    Bewusst EIGENER Alarmweg statt ``blocking_reason``: dort konkurriert der
    Grund mit Dutzenden anderen Sperrgruenden (Mindestpause,
    Start-Antizipation, ...), was zu Alarm-Wechseln und Spam fuehrt. Hier
    wird stattdessen ueber den bestehenden Drosselmechanismus gesteuert.

    Drosselung: alle ``_INTERVALLE_MIN`` eine Meldung - nicht nur einmal.
    Ein Ausfall, der nach 20 Minuten von allein endet, und einer, der den
    ganzen Tag laeuft, muessen unterschiedlich behandelt werden. Die erste
    Meldung kommt sofort; die Wiederholung erst spaeter, damit ein langer
    Ausfall sichtbar bleibt, ohne Telegram zu fluten.
    """
    # Zaehler defensiv: ein MagicMock (Tests) oder ein Fremdtyp darf den
    # Datenpfad nicht mit einem TypeError zerstoeren.
    try:
        vorher = int(getattr(state, "solar_stale_meldungen", 0) or 0)
    except (TypeError, ValueError):
        vorher = 0
    state.solar_stale_meldungen = vorher + 1
    # Erste Meldung sofort, danach im Intervall.
    if state.solar_stale_meldungen > 1 and not check_log_throttle(
        state, "_alarm_solar_stale", interval_minutes=STALE_ALARM_MINUTEN
    ):
        return
    if not getattr(state, "bot_token", None):
        return
    alter_txt = (
        f"seit {alter_min:.0f} min" if alter_min is not None else "seit unbekannt"
    )
    try:
        await control_logic.send_telegram_message(
            session,
            state.config.Telegram.CHAT_ID,
            "☁️ *Solax-Daten nicht erreichbar*\n"
            f"Alter: {alter_txt} (Grenze {SOLAR_DATA_STALE_THRESHOLD_MIN} min)\n\n"
            "Die PV-, Batterie-, Einspeisungs- und CalcStart-Regeln sind\n"
            "*pausiert*. Es heizen nur noch Abweichung, Mindesttemperatur,\n"
            "Notfallschutz und Legionellen – *ohne Quellenprüfung, also aus\n"
            "dem Netz*.\n\n"
            "Sobald die API wieder antwortet, melde ich mich.",
            state.config.Telegram.BOT_TOKEN,
            parse_mode="Markdown",
        )
    except Exception:
        logging.exception("Solar-Ausfall: Telegram-Alarm fehlgeschlagen")
    logging.error(
        f"Solar-Ausfall gemeldet (Meldung {state.solar_stale_meldungen}, "
        f"PV-Regeln pausiert, verbleibende Heizung aus dem Netz)"
    )


async def _solar_ausfall_zurueck_melden(session, state):
    """Einmalige Entwarnung nach dem Ende eines Solar-Ausfalls."""
    if getattr(state, "solar_stale", False) is not False:
        return
    if not getattr(state, "solar_stale_meldungen", 0):
        return
    anzahl = state.solar_stale_meldungen
    state.solar_stale_meldungen = 0
    meldungen = "1 Alarm" if anzahl == 1 else f"{anzahl} Alarme"
    logging.info(
        f"Solax-Daten wieder frisch nach {meldungen} - PV-Regeln laufen wieder"
    )
    if not getattr(state, "bot_token", None):
        return
    try:
        await control_logic.send_telegram_message(
            session,
            state.config.Telegram.CHAT_ID,
            "☀️ *Solax-Daten wieder da.*\n"
            "Die PV-, Batterie- und Einspeisungsregeln laufen wieder "
            f"({meldungen} während des Ausfalls).",
            state.config.Telegram.BOT_TOKEN,
            parse_mode="Markdown",
        )
    except Exception:
        logging.exception("Solar-Erholung: Telegram-Meldung fehlgeschlagen")
