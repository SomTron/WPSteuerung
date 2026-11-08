"""Legionellen-Regel (Prophylaxe 1x/Woche).

Ausgelagert aus priority_control.py. Re-Export in priority_control.py.
"""
from typing import Optional, Dict
from datetime import datetime, timedelta

from rule_types import RegelErgebnis
from regel_helpers import _is_nachtsperre, _is_weekend, _legionellen_quelle_status, _legionellen_ueberfaellig, _wochentag_name

def evaluate_legionellen(
    legionellen_cfg: object,
    temp_dict: Dict[str, Optional[float]],
    now: datetime,
    legionellen_aktiv: bool = False,
    legionellen_last_done: Optional[datetime] = None,
    legionellen_started_at: Optional[datetime] = None,
    legionellen_unvollstaendig: bool = False,
    kompressor_ein: bool = False,
    legionellen_target_reached_at: Optional[datetime] = None,
    wochenende_cfg: object = None,
    legionellen_planned_tag: Optional[int] = None,
    legionellen_planned_date: object = None,
    pv_leistung: Optional[float] = None,
    pv_acpower: Optional[float] = None,
    battery_power: Optional[float] = None,
    soc: Optional[float] = None,
    solar_stale: bool = False,
    # BUGFIX (21.10.2026): Ausserhalb des PV-Fensters zaehlt PV nicht
    # als Quelle - auch nicht fuer die Legionellenprophylaxe.
    pv_quelle_erlaubt: bool = True,
    # NUTZERANFORDERUNG (23.10.2026): Die Legionellenprophylaxe haelt sich
    # an die Nachtsperre - auch eine BEREITS LAUFENDE Fahrt. Sie wird nachts
    # nicht weitergeheizt, sondern am naechsten Tag als Nachversuch
    # fortgesetzt. Defaults entsprechen SicherheitConfig (19-8 Uhr).
    nachtsperre_start: int = 19,
    nachtsperre_ende: int = 8,
) -> RegelErgebnis:
    """Bewertet, ob die Legionellenprophylaxe durchgefuehrt werden soll."""
    result = RegelErgebnis(
            name="Legionellen",
            prioritaet=legionellen_cfg.prioritaet,
            aktiv=True,
        )
    if not legionellen_cfg.aktiv:
        result.aktiv = False
        result.grund = "Legionellenprophylaxe deaktiviert"
        return result
    t_unten = temp_dict.get("unten")
    if t_unten is None:
        result.aktiv = False
        result.grund = "Sensordaten nicht verfuegbar"
        return result
    # Nachtsperre (Nutzeranforderung 23.10.2026): Die Prophylaxe respektiert
    # die Nachtsperre wie jede andere Regel - auch eine laufende Fahrt.
    # Der Grund steht VOR allen anderen Zweigen, weil die Sperre sonst von
    # "Legionellen aktiv ... heize weiter" ueberstimmt wuerde (die Regel ist
    # mit Prio 90 Gewinner und lief frueher die ganze Nacht durch).
    # Der Hardware-Stopp merkt den Lauf als `legionellen_unvollstaendig` vor;
    # der Plan bleibt stehen und der Nachversuch startet am naechsten Tag.
    if _is_nachtsperre(
        int(now.hour), int(nachtsperre_start), int(nachtsperre_ende)
    ):
        result.aktiv = False
        result.einschalten = None
        result.grund = (
            f"Nachtsperre aktiv ({int(nachtsperre_start)}-"
            f"{int(nachtsperre_ende)} Uhr) - Legionellenprophylaxe pausiert"
            + (
                "; Nachversuch am naechsten Tag"
                if (legionellen_aktiv or legionellen_unvollstaendig)
                else ""
            )
        )
        return result
    if legionellen_aktiv:
        # NUTZERANFORDERUNG (25.10.2026): Ein einmaliges Erreichen von 60 C
        # unten genuegt. Danach wird NICHT weitergeheizt (frueher lief die
        # Heizung 30 min weiter und trieb den Speicher bis 65 C). Stattdessen
        # laeuft eine Halteprobe: 10 min lang mindestens 55 C unten.
        try:
            ziel_c = float(legionellen_cfg.target_temp_c)
            probe_min_c = float(
                getattr(legionellen_cfg, "probezeit_min_temp_c", 55.0)
            )
            probe_min = max(0, int(legionellen_cfg.probezeit_minuten))
        except (AttributeError, TypeError, ValueError, OverflowError):
            ziel_c = 60.0
            probe_min_c = 55.0
            probe_min = 10
        if legionellen_target_reached_at is not None:
            # Halteprobe laeuft: heizen nur, wenn die Mindesttemperatur faellt.
            if t_unten < probe_min_c:
                result.einschalten = True
                result.grund = (
                    f"Legionellen: Halteprobe unterbrochen - unten {t_unten:.1f}C "
                    f"< {probe_min_c:.0f}C, heize erneut auf {ziel_c:.0f}C"
                )
                return result
            try:
                gehalten_seit = now - legionellen_target_reached_at
            except (TypeError, ValueError):
                gehalten_seit = timedelta(0)
            if gehalten_seit < timedelta(minutes=probe_min):
                result.einschalten = False
                verbleib = max(
                    0,
                    int(
                        (
                            timedelta(minutes=probe_min) - gehalten_seit
                        ).total_seconds() // 60
                    )
                    + 1,
                )
                result.grund = (
                    f"Legionellen: Ziel {ziel_c:.0f}C erreicht - Heizen aus; "
                    f"Halteprobe laeuft noch (ca. {verbleib} min, min "
                    f"{probe_min_c:.0f}C)"
                )
            else:
                result.einschalten = False
                result.grund = (
                    f"Legionellen: Ziel {ziel_c:.0f}C einmalig erreicht und "
                    f"{probe_min} min Halteprobe >= {probe_min_c:.0f}C "
                    f"abgeschlossen -> AUS"
                )
            return result
        if t_unten >= ziel_c:
            if legionellen_started_at is None:
                result.einschalten = None
                result.grund = "Legionellen wartet: Startzeit fehlt fuer die Halteprobe"
                return result
            # Zieltemperatur einmalig erreicht -> Heizen stoppt sofort; die
            # Halteprobe uebernimmt der Lifecycle (Zeitstempel setzen).
            result.einschalten = False
            result.grund = (
                f"Legionellen: Ziel {ziel_c:.0f}C unten erreicht - Heizen "
                f"stoppt, Halteprobe {probe_min} min (min {probe_min_c:.0f}C) "
                f"startet"
            )
            return result
        result.einschalten = True
        result.grund = (
            f"Legionellen aktiv: {t_unten:.1f}C < {ziel_c:.0f}C, heize weiter"
        )
        return result
    # Nachversuch einer abgebrochenen Fahrt: Das Wochentags-Gate und das
    # Startfenster werden dann uebersprungen, damit die Prophylaxe nicht
    # still ausfaellt. Begrenzt wird der Nachversuch weiterhin durch
    # `hygiene_notfall` / die Frist - er kann also nicht endlos laufen.
    nachversuch = bool(legionellen_unvollstaendig)
    if legionellen_last_done is not None and not nachversuch:
        letzte_kw = legionellen_last_done.isocalendar()[1]
        aktuelle_kw = now.isocalendar()[1]
        if letzte_kw == aktuelle_kw:
            result.aktiv = False
            result.grund = f"Bereits in KW {aktuelle_kw} durchgefuehrt"
            return result
    start_h = int(legionellen_cfg.start_uhr)
    is_weekend = _is_weekend(now)
    fruehestens = (
        getattr(wochenende_cfg, "fruehestens_uhr", None)
        if wochenende_cfg is not None
        else None
    )
    heute_wochentag = now.weekday()  # 0=Mo .. 6=So

    # Hygiene-Frist: Ist der letzte Lauf zu lange her, werden Plan- und
    # Wochentags-Gate geloest. Sonst koennte in einer PV-armen Winterwoche
    # weder der Plan noch der Wochentag zu einem Start kommen - die
    # Prophylaxe faellt dann still aus.
    hygiene_notfall = _legionellen_ueberfaellig(
        legionellen_cfg, legionellen_last_done, now
    )

    # --- Wochentags-Gate (Nutzeranforderung) ---
    # Start nur an einem der erlaubten Tage (bevorzugter_tag..letzter_tag,
    # z.B. Freitag..Sonntag). Liegt bereits ein geplanter bester Forecast-Tag
    # vor, wird nur noch an EXAKT diesem Tag gestartet. An allen anderen Tagen
    # bleibt die Regel stumm/inaktiv (kein falscher Start am Mittwoch etc.).
    erlaubt_tag = range(
        int(legionellen_cfg.bevorzugter_tag),
        int(legionellen_cfg.letzter_tag) + 1,
    )
    # NUTZERANFORDERUNG (23.10.2026): Ein offener Nachversuch darf NICHT auf
    # einen spaeter geplanten Termin warten. Genau das passierte nach einer
    # Nachtunterbrechung: die Tagesplanung setzte den Plan auf den naechsten
    # besten PV-Tag (z.B. Sonntag), und dieser Zweig blockierte die
    # Fortsetzung am Folgetag. Der Nachversuch setzt daher vor dem Plan an.
    if (
        legionellen_planned_date is not None
        and now.date() < legionellen_planned_date
        and not nachversuch
    ):
        result.aktiv = True
        result.einschalten = None
        result.grund = (
            f"Legionellen: geplanter Termin {legionellen_planned_date} liegt in der Zukunft; "
            "warte auf den Plan"
        )
        # Eigener Code: das Warten auf einen in der Zukunft geplanten Termin ist
        # KEIN Warten auf eine gerade verfuegbare Quelle. Sonst waehlte
        # `determine_mode_and_setpoints` die Regel ueber den Code
        # "waiting_source" als "Gewuenschte Regel" - obwohl die Prophylaxe erst
        # in Tagen startet und heute nichts anfordert (Befund 14.10.2026).
        result.set_reason_code("planned_future")
        return result
    if legionellen_planned_date is not None and now.date() > legionellen_planned_date and not nachversuch:
        result.set_reason_code("planned_day_missed")
        result.grund = (
            f"Legionellen: geplanter Termin {legionellen_planned_date} ist verstrichen; "
            "naechster geeigneter PV-Tag wird geplant"
        )
        return result
    if legionellen_planned_tag is not None and not nachversuch:
        if heute_wochentag != int(legionellen_planned_tag) and not hygiene_notfall:
            result.aktiv = True
            result.einschalten = None
            result.grund = (
                f"Legionellen: Start erst am geplanten Tag "
                f"({legionellen_planned_tag}) - heute ist "
                f"{_wochentag_name(heute_wochentag)}"
            )
            return result
    elif heute_wochentag not in erlaubt_tag and not hygiene_notfall and not nachversuch:
        result.aktiv = True
        result.einschalten = None
        result.grund = (
            f"Legionellen: Nur Start an {_wochentag_name(int(legionellen_cfg.bevorzugter_tag))}"
            f"-{_wochentag_name(int(legionellen_cfg.letzter_tag))} "
            f"(heute: {_wochentag_name(heute_wochentag)})"
        )
        return result

    # Ein gemeinsames Startfenster gilt an Werktagen und am Wochenende.
    # Innerhalb des Fensters wartet die Regel auf PV/Batterie; nach dem
    # spaetesten Start wird der Tag fuer Variante C als verpasst markiert.
    spaeteste_start_h = int(getattr(legionellen_cfg, "spaeteste_start_uhr", 12))
    frueheste_start_h = start_h
    if is_weekend and fruehestens is not None:
        frueheste_start_h = max(frueheste_start_h, int(fruehestens))
    aktuelle_zeit = now.hour + now.minute / 60.0
    if aktuelle_zeit < frueheste_start_h and not nachversuch:
        result.aktiv = False
        result.grund = (
            f"Legionellen: vor Startfenster {frueheste_start_h:g}:00"
            + (" (Wochenendfreigabe)" if frueheste_start_h > start_h else "")
        )
        return result
    if now.hour > spaeteste_start_h and not hygiene_notfall and not nachversuch:
        result.set_reason_code("planned_day_missed")
        result.grund = (
            f"Legionellen: Startfenster {frueheste_start_h:g}:00-"
            f"{spaeteste_start_h:g}:00 verpasst; naechster geeigneter PV-Tag"
        )
        return result
    if kompressor_ein:
        result.einschalten = None
        result.grund = "Kompressor laeuft bereits, warte auf Abschluss"
        return result
    # Hygiene-Notfall: Ist der letzte Lauf laänger her als die konfigurierte
    # Frist, wird die Quelle zurueckgestellt. Ohne diese Ausnahme bleibt die
    # Prophylaxe in einer PV- armen Winterwoche komplett aus - der Speicher
    # erreicht 60 C nie und das Legionella-Risiko waechst ungebremst.
    ueberfaellig = _legionellen_ueberfaellig(legionellen_cfg, legionellen_last_done, now)
    if ueberfaellig:
        quelle_ok, quelle_text = True, "Hygiene-Notfall (Frist ueberschritten, Netzbetrieb)"
    else:
        quelle_ok, quelle_text = _legionellen_quelle_status(
            legionellen_cfg, pv_leistung if pv_leistung is not None else 0.0,
            battery_power, soc, solar_stale,
            pv_acpower, pv_erlaubt=pv_quelle_erlaubt
        )
    if not quelle_ok:
        result.einschalten = None
        result.set_reason_code("waiting_source")
        result.grund = f"Legionellen wartet auf PV/Batterie: {quelle_text}"
        return result
    result.einschalten = True
    result.grund = (
        f"Legionellenprophylaxe faellig: Starte mit {quelle_text} auf "
        f"{legionellen_cfg.target_temp_c:.0f}C (unten {t_unten:.1f}C)"
    )
    return result


