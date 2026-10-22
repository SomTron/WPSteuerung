"""Lebenszyklus der Legionellenprophylaxe.

BUGFIX (22.10.2026): Aus main.py herausgezogen. Die vier Funktionen
bilden eine geschlossene Gruppe - sie brauchen nur `state`, den
Telegram-Versand und zwei Helfer aus clock/utils. In main.py lagen
sie ueber drei weit entfernte Stellen verteilt (Hardware-Setup,
Perioden-Task, Alarmierung) - das hat die Zusammengehoerigkeit
unsichtbar gemacht.

Die Logik ist 1:1 verschoben.
"""
import logging
from datetime import datetime, timedelta

import priority_control_logic as pcl
from clock import now_for
from legionellen_plan import clear_plan
from utils import safe_timedelta


def _beende_legionellenlauf_if_needed(state, now, end_grund=None) -> None:
    """Bereinigt den Legionellen-Lifecycle bei jedem echten Hardware-Stopp.

    Ein Abbruch vor Erreichen der Zieltemperatur ist KEIN erfolgreicher
    Abschluss (Befund aus dem Laufzeitlog 18.09.-01.10.2026: am 25.09. drei
    Anlaeufe bis max. 53,0 C bei Ziel 60 C, jeder Abbruch raeumte Plan und
    Flag auf). Deshalb wird ein solcher Abbruch als `unvollstaendig`
    vorgemerkt und der Plan beibehalten - der erneute Start ist dann in
    evaluate_legionellen ausdruecklich zulaessig.
    """
    if getattr(state, "legionellen_aktiv", False) is not True:
        return
    cfg = getattr(getattr(state, "priority_config", None), "legionellen", None)
    target_at = getattr(state, "legionellen_target_reached_at", None)
    probe_done = False
    if cfg is not None and isinstance(target_at, datetime):
        try:
            probe_done = safe_timedelta(now, target_at, state.local_tz) >= timedelta(
                minutes=float(cfg.probezeit_minuten)
            )
        except (TypeError, ValueError, OverflowError):
            probe_done = False
    # Der korrekte Lifecycle-Status wird an der einen Stelle bereinigt.
    state.legionellen_aktiv = False
    state.legionellen_temp_override = None
    state.legionellen_started_at = None
    # Bei bereits erfüllter Probezeit bleibt der Zielzeitpunkt für den
    # nachgelagerten Lifecycle-Abschluss erhalten.
    if not probe_done:
        state.legionellen_target_reached_at = None
    state.legionellen_end_time = now
    state._last_was_legionellen = True
    state._legionellen_completion_pending = probe_done
    if probe_done:
        # Erfolgreich: KW ist erledigt, Plan wird hier aufgeraeumt.
        try:
            clear_plan(state, f"Legionellenlauf beendet: {end_grund or 'Stop'}", persist=True)
        except Exception:
            logging.debug("Legionellenplan konnte beim Laufende nicht bereinigt werden", exc_info=True)
    else:
        # ABGEBROCHEN vor Erreichen des Ziels. Vorher wurden Plan und Flag
        # bedingungslos aufgeraeumt - dadurch konnte die Prophylaxe still
        # ausfallen (Log 18.09.-01.10.2026: 25.09. drei Anlaeufe bis max.
        # 53,0 C bei Ziel 60 C; naechster Versuch erst am Folgetag kalt).
        # Jetzt: als unvollstaendig vormerken, Plan BEHALTEN. Nur so ist ein
        # erneuter Start moeglich (Ausnahme in evaluate_legionellen).
        state.legionellen_unvollstaendig = True
        logging.warning(
            "Legionellenlauf vor dem Ziel abgebrochen (%s, Ziel %.0fC nicht erreicht) - "
            "unvollstaendig vorgemerkt, erneuter Anlauf zulaessig.",
            end_grund or "Stop",
            float(getattr(cfg, "target_temp_c", 60.0) or 60.0),
        )


def _complete_legionellen_lifecycle(state, now) -> None:
    """Markiert einen erfolgreichen Probeabschluss ohne effektive Hardwarefelder."""
    state.legionellen_last_done = now.date()
    state.legionellen_wochennummer = now.isocalendar()[1]
    state.legionellen_aktiv = False
    # Ziel und Probezeit erreicht: die Fahrt gilt als vollstaendig, eine
    # eventuell gemerkte Unvollstaendigkeit aus einem früheren Abbruch ist
    # damit erledigt.
    state.legionellen_unvollstaendig = False
    state.legionellen_temp_override = None
    state.legionellen_started_at = None
    state.legionellen_target_reached_at = None
    state.legionellen_end_time = now
    state._last_was_legionellen = True
    state._legionellen_completion_pending = False
    state.control.requested_rule_name = "Legionellen"
    # Nach dem Hardware-Stopp dürfen keine effektiven Felder reaktiviert werden.
    state.control.effective_rule_name = None
    state.control.active_rule_name = None
    state.control.effective_source = None
    state.control.source_at_start = None
    state.control.source_current = None
    state.control._lauf_start_regel = None
    try:
        clear_plan(state, "Legionellenlauf abgeschlossen", persist=True)
    except Exception:
        logging.debug("Legionellenplan konnte nach Abschluss nicht gelöscht werden", exc_info=True)


def _legionellen_bedarf_wh(legionellen_cfg, state, flaeche_qm: float):
    """Energiebedarf fuer eine vollstaendige Legionellenfahrt (in Wh).

    BUGFIX (21.10.2026): Die Tageswahl orientierte sich bisher nur an der
    ROHEN Prognose. Die Nutzerfrage war aber, ob der Lauf an einem Tag
    UEBERHAUPT fertig wird: "ist der Forecast gut genug, um den Lauf auch
    am Freitag fertig zu bekommen?"

    Der Bedarf setzt sich zusammen aus:
      * Aufheizenergie: (Zieltemperatur - aktuelle Temperatur) x
        Speichervolumen x spezifische Waermekapazitaet (vereinfacht:
        1 kWh pro 10 K und 100 L),
      * Probezeit: die Haltezeit nach Erreichen der Zieltemperatur.

    Der aktuelle Speicherzustand wird ueber `state` genommen - ohne ihn
    waere der Bedarf nicht berechenbar.

    Rueckgabe: Bedarf in Wh oder None, wenn nicht berechenbar (dann
    entscheidet die Rohprognose).
    """
    try:
        ziel_c = float(legionellen_cfg.target_temp_c)
        probezeit_min = float(getattr(legionellen_cfg, "probezeit_minuten", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return None
    if ziel_c <= 0:
        return None

    # Aktueller Speicherzustand: der untere Fuehler ist der Bezugsfuehler
    # (kaltester Punkt, Bezugsfuehler fuer das Boiler-Maximum).
    aktuell_c = None
    try:
        aktuell_c = getattr(state.sensors, "t_unten", None)
        if aktuell_c is None:
            aktuell_c = getattr(state.sensors, "t_oben", None)
    except AttributeError:
        aktuell_c = None
    if not isinstance(aktuell_c, (int, float)) or isinstance(aktuell_c, bool):
        return None
    aktuell_c = float(aktuell_c)

    # Speichervolumen: aus der Boiler-Konfiguration, sonst der Standard
    # des Analyse-Tools (300 L).
    volumen_l = 300.0
    try:
        boiler = getattr(getattr(state, "config", None), "Boiler", None)
        if boiler is not None:
            wert = getattr(boiler, "VOLUMEN_L", None)
            if isinstance(wert, (int, float)) and not isinstance(wert, bool):
                volumen_l = float(wert)
    except AttributeError:
        pass
    if volumen_l <= 0:
        volumen_l = 300.0

    # Waermekapazitaet des Wassers: 1,163 Wh/(l*K). Vereinfacht wird die
    # Speichermasse (Wasser + Boiler) mit dem Faktor 1,2 gerechnet.
    delta_k = max(0.0, ziel_c - aktuell_c)
    aufheiz_wh = delta_k * volumen_l * 1.163 * 1.2
    # Die Probezeit (Halten der Zieltemperatur) kostet zusaetzlich Energie -
    # bei 60 C und 30 min sind das rund 30 l * 60 K * 1,163 Wh/(l*K) / 60
    # min/h = 35 Wh. Der genaue Wert haengt am Lastgang; konservativ
    # gerechnet wird mit dem Anteil am Tagesbedarf.
    probe_wh = 0.0
    if probezeit_min > 0:
        probe_wh = (volumen_l * 1.163 * max(0.0, ziel_c - 30.0)
                    * (probezeit_min / 60.0))
    return aufheiz_wh + probe_wh


async def _aktualisiere_legionellen_lifecycle(session, state, result):
    """Startet, ueberwacht und beendet die Legionellenfahrt nach dem Hardware-Start."""
    cfg = state.priority_config.legionellen
    if not cfg.aktiv:
        return
    now = now_for(state)
    if getattr(state, "_legionellen_completion_pending", False):
        _complete_legionellen_lifecycle(state, now)
        logging.info(
            "Legionellenprophylaxe ABGESCHLOSSEN: KW %d, Ziel %.0fC inklusive %d min Probezeit",
            now.isocalendar()[1], cfg.target_temp_c, cfg.probezeit_minuten,
        )
        return
    winner = result.get("gewinner_ergebnis")
    if not state.legionellen_aktiv and state.legionellen_temp_override is not None:
        state.legionellen_temp_override = None
    if winner is None or winner.name != "Legionellen":
        return
    if (
        winner.einschalten is True
        and not state.legionellen_aktiv
        and state.control.kompressor_ein
        and getattr(state.control, "_lauf_start_regel", None) == "Legionellen"
    ):
        state.legionellen_aktiv = True
        state.legionellen_started_at = now
        state.legionellen_target_reached_at = None
        state.legionellen_telegram_start_sent = False
        state.legionellen_telegram_done_sent = False
        state.legionellen_temp_override = cfg.legionellen_max_temp_c
        state.control.requested_rule_name = "Legionellen"
        state.control.effective_rule_name = "Legionellen"
        state.control.effective_source = pcl._aktuelle_quellenbezeichnung(state)
        logging.info(
            "Legionellenprophylaxe GESTARTET: Heize auf %.0fC (max %.0fC)",
            cfg.target_temp_c, cfg.legionellen_max_temp_c,
        )
        if not state.legionellen_telegram_start_sent:
            try:
                msg = (f"🦠 *Legionellenprophylaxe gestartet!*\n"
                       f"Heize auf {cfg.target_temp_c:.0f}°C "
                       f"(unten: {state.sensors.t_unten:.1f}°C)")
                from telegram_api import send_telegram_message as _send_tg
                await _send_tg(session, state.config.Telegram.CHAT_ID, msg,
                               state.config.Telegram.BOT_TOKEN, parse_mode="Markdown")
                state.legionellen_telegram_start_sent = True
            except Exception as exc:
                logging.warning(f"Legionellen-Telegram-Start fehlgeschlagen: {exc}")
        return

    if not state.legionellen_aktiv:
        return

    started = state.legionellen_started_at
    # Zeitlimit als Notausstieg. 0 = kein Limit (Nutzeranforderung
    # 16.10.2026: das 8-h-Limit brach eine Fahrt bei unten 55,9 C ab, Ziel
    # war 60 C). Die Sicherheitsgrenzen (Boiler-Max 65 C, Ueberhitzungs-
    # schutz) begrenzen die Fahrt unabhaengig davon.
    limit_h = getattr(cfg, "max_duration_hours", 0)
    if not isinstance(limit_h, (int, float)) or isinstance(limit_h, bool):
        limit_h = 0
    if float(limit_h) > 0 and started is not None:
        try:
            elapsed = safe_timedelta(now, started, state.local_tz)
        except (TypeError, ValueError):
            elapsed = timedelta(0)
        if elapsed >= timedelta(hours=float(limit_h)):
            # Ziel NICHT erreicht -> wie jeden anderen Abbruch vor dem Ziel
            # behandeln: unvollstaendig vormerken und den Plan behalten, damit
            # ein Nachversuch moeglich bleibt. Wuerde hier (wie frueher) der
            # Plan geloescht, faellt die Prophylaxe still fuer die ganze
            # Kalenderwoche aus - genau im Moment des Abbruchs.
            state.legionellen_aktiv = False
            state.legionellen_temp_override = None
            state.legionellen_started_at = None
            state.legionellen_target_reached_at = None
            state.legionellen_end_time = now
            state.legionellen_unvollstaendig = True
            state._last_was_legionellen = True
            _t_unten = getattr(getattr(state, "sensors", None), "t_unten", None)
            _t_unten_txt = (
                f"{float(_t_unten):.1f}C"
                if isinstance(_t_unten, (int, float)) and not isinstance(_t_unten, bool)
                else "n/a"
            )
            logging.warning(
                "Legionellenprophylaxe ABGEBROCHEN (Zeitlimit %.1fh >= %.1fh, "
                "Ziel %.0fC nicht erreicht, unten %s) - unvollstaendig "
                "vorgemerkt, erneuter Anlauf zulaessig.",
                elapsed.total_seconds() / 3600.0, float(limit_h),
                float(getattr(cfg, "target_temp_c", 60.0) or 60.0),
                _t_unten_txt,
            )
            return

    t_unten = state.sensors.t_unten
    if (
        isinstance(t_unten, (int, float))
        and t_unten >= cfg.target_temp_c
        and state.legionellen_target_reached_at is None
    ):
        state.legionellen_target_reached_at = now
        logging.info(
            "Legionellen: Zieltemperatur erreicht, Probezeit startet (%d min)",
            cfg.probezeit_minuten,
        )

    if winner.einschalten is not False or state.legionellen_target_reached_at is None:
        return
    try:
        probe_done = safe_timedelta(
            now, state.legionellen_target_reached_at, state.local_tz
        ) >= timedelta(minutes=cfg.probezeit_minuten)
    except (TypeError, ValueError):
        probe_done = False
    if not probe_done:
        return

    state.legionellen_last_done = now.date()
    state.legionellen_wochennummer = now.isocalendar()[1]
    state.legionellen_aktiv = False
    state.legionellen_temp_override = None
    state.legionellen_started_at = None
    state.legionellen_target_reached_at = None
    state.legionellen_end_time = now
    clear_plan(state, "Legionellenlauf abgeschlossen", persist=True)
    state._last_was_legionellen = True
    # Nach dem Abschluss dürfen effektive Hardwarefelder nicht wieder
    # resurrectiert werden; requested_rule_name bleibt als Diagnose erhalten.
    logging.info(
        "Legionellenprophylaxe ABGESCHLOSSEN: KW %d, Ziel %.0fC inklusive %d min Probezeit",
        now.isocalendar()[1], cfg.target_temp_c, cfg.probezeit_minuten,
    )
    if not state.legionellen_telegram_done_sent:
        try:
            msg = (f"✅ *Legionellenprophylaxe abgeschlossen!*\n"
                   f"KW {now.isocalendar()[1]}: {cfg.target_temp_c:.0f}°C und Probezeit erreicht")
            from telegram_api import send_telegram_message as _send_tg
            sent = await _send_tg(session, state.config.Telegram.CHAT_ID, msg,
                                  state.config.Telegram.BOT_TOKEN, parse_mode="Markdown")
            if sent is not False:
                state.legionellen_telegram_done_sent = True
        except Exception as exc:
            logging.warning(f"Legionellen-Telegram-Done fehlgeschlagen: {exc}")
