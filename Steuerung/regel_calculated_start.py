"""CalcStart-Regel (berechneter Start fuer gelernte Zapf-Zeit).

Ausgelagert aus priority_control.py. Re-Export in priority_control.py.
"""
from typing import Optional, Dict, List

from rule_types import RegelErgebnis
from json_config import CalculatedStartConfig
from regel_helpers import _debug_once, _energiequelle_mit_grund, _etappen_plan, _is_nachtsperre, _mittagstief_stunden, _parse_sensor, _uhr_text, calcstart_nachtsperre_konflikt

def evaluate_calculated_start(
    calc_cfg: CalculatedStartConfig,
    temp_dict: Dict[str, Optional[float]],
    now_hour: int,
    now_minute: int,
    nachtsperre_start: int = 19,
    nachtsperre_ende: int = 8,
    forecast_wh_qm: Optional[float] = None,
    learned_heating_rate_unten: Optional[float] = None,
    learned_heating_rate_gesamt: Optional[float] = None,
    learned_target_hour: Optional[float] = None,
    feedin_watt: float = 0.0,
    soc: Optional[float] = None,
    fc_ratio: float = 1.0,
    surplus_profile: Optional[Dict[str, float]] = None,
    recent_usage_events: Optional[List[Dict]] = None,
    pv_acpower: Optional[float] = None,
    battery_power: Optional[float] = None,
    solar_stale: bool = False,
    pv_fenster_stunde: Optional[float] = None,
    pv_fenster_aktiv: bool = True,
    pv_quelle_erlaubt: bool = True,
    pv_fenster_reserve_h: float = 0.5,
    # BUGFIX (22.10.2026): Zapfungs-Vorhaltung.
    #
    # Aus der gelernten Zapfungsmenge laesst sich berechnen, wie viel
    # der Speicher nach der Zapfung kalt ist:
    #     dT = Q * (T_speicher - T_kaltwasser) / (rho * c_p * V_speicher)
    # Beispiel (Live-Fall 15.10.2026): 150 l aus einem 300-l-Speicher
    # mit 42 C, Kaltwasser 10 C -> der Speicher verliert ca. 5,7 K.
    # Ohne diese Vorhaltung bleibt "unten" nach der Zapfung kalt - die
    # WP kann den Speicher von unten nicht mehr fuellen (oben ist voll).
    zapf_vorhaltung_k: float = 0.0,
    zapfmenge_l: float = 0.0,
    speicher_volumen_l: float = 300.0,
    kaltwasser_c: float = 10.0,
    # Aufteilung in Mittag und Abend (Nutzerwunsch).
    name_override: Optional[str] = None,
    target_uhr_override: Optional[int] = None,
    etappen_filter: Optional[List[str]] = None,
) -> RegelErgebnis:
    """
    Berechnete-Startzeit-Regel: Schaltet rechtzeitig vor der Zielzeit ein.

    Berechnet aus Temperaturdifferenz und Heizrate die benötigte Zeit.
    Wenn die verbleibende Zeit knapp wird -> einschalten.

    ``pv_fenster_stunde`` (Nutzerfrage 18.10.2026): letzte Stunde mit
    erwarteter PV ueber der Schwelle, aus der stuendlichen Prognose. Der
    Spaetest-Start wird dann auf ``Fensterende - pv_fenster_reserve_min``
    vorgezogen, damit der Lauf noch aus PV gedeckt ist; ohne diesen Wert
    (keine Prognose) bleibt das Verhalten unveraendert.

    ``pv_fenster_aktiv`` = Master-Schalter der Fensterlogik (Config).
    ``pv_quelle_erlaubt`` = ob die PV-Momentanmessung gerade als Quelle
    zaehlen darf (False = ausserhalb des Fensters).
    """
    _name = name_override if name_override is not None else "CalcStart"
    _target_uhr = target_uhr_override if target_uhr_override is not None else calc_cfg.target_uhr

    result = RegelErgebnis(
        name=_name, prioritaet=calc_cfg.prioritaet, aktiv=calc_cfg.aktiv
    )

    if not calc_cfg.aktiv:
        result.grund = "CalcStart-Regel inaktiv"
        return result

    # Nachtsperre: Kein Einschalten waehrend der Sperrzeit
    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)
    if nachtsperre:
        result.aktiv = False
        # Konflikt-Hinweis: Liegt die Zielzeit hinter/innerhalb der Sperre,
        # kann die Regel NIE feuern - das soll im Grund lesbar sein.
        ziel = (
            learned_target_hour
            if learned_target_hour is not None
            else float(_target_uhr)
        )
        tot, _, hinweis = calcstart_nachtsperre_konflikt(
            ziel, nachtsperre_start, nachtsperre_ende
        )
        result.grund = "Nachtsperre aktiv" + (f" | {hinweis}" if tot else "")
        return result

    temp_unten = _parse_sensor(temp_dict, "unten")
    temp_mitte = _parse_sensor(temp_dict, "mitte")
    temp_oben = _parse_sensor(temp_dict, "oben")

    if temp_unten is None or temp_mitte is None:
        result.aktiv = False
        result.grund = "Sensoren nicht verfuegbar"
        return result

    # Bereits erreicht? Nur unteren Fuehler pruefen (kaltester Punkt im Boiler)
    # Der obere/mittige Fuehler kann durch Schichtung heiss sein, obwohl
    # die WP noch viel Energie unten reinstecken kann (vor allem bei PV!)
    if temp_unten is not None and temp_unten >= calc_cfg.tmax_c:
        result.einschalten = False
        result.grund = (
            f"CalcStart: Zieltemp {calc_cfg.tmax_c}C unten bereits erreicht -> AUS"
        )
        return result

    # Aktuelle Zeit
    current_time = now_hour + now_minute / 60.0

    # BEFUND 04.10.2026: Die Zielzeit wird ZWEIMAL bestimmt, und die beiden
    # Stellen unterschieden sich:
    #   * hier (Nachtsperre-Hinweis) und in allen folgenden Rechnungen
    #     `ziel_uhr` - die GELERNTE Zielzeit (Default 17:00),
    #   * in der Abbruchbedingung "Nach Zielzeit" dagegen die KONFIGURIERTE
    #     `calc_cfg.target_uhr`.
    # Nachgerechnet mit gelernter Abend-Zielzeit 18:30 und konfiguriert
    # 17:00: ab 17:00 meldete die Regel "Nach Zielzeit" und war damit tot -
    # genau waehrend der Zeit, fuer die die Zapfgarantie stehen soll. Die
    # Regel selbst hatte vorher (16:45) noch "Puffer reicht -> warte"
    # entschieden, weil sie auf die 18:30-Zielzeit rechnete.
    #
    # Umgekehrt erzwingt eine zu FRUEH gelernte Zielzeit (14:00) von 11:00
    # bis 17:00 durchgehend "ZU SPAET! -> EIN (Notfall)".
    #
    # Die Zielzeit wird deshalb genau einmal bestimmt und fuer alles
    # verwendet - Abbruch, Restzeit und Meldungen.
    #
    # BEFUND 09.11.2026: Mit den getrennten Regeln CalcStart-Mittag und
    # CalcStart-Abend gewann hier IMMER `learned_target_hour` - die gelernte
    # ABEND-Zapfzeit (Default 17:00) - auch fuer den Mittag. Im else-Zweig
    # wurde zusaetzlich `calc_cfg.target_uhr` statt `_target_uhr` verwendet,
    # sodass `target_mittag_uhr` NIE ankam. Folge im Betriebslog: beide
    # Regeln meldeten wortgleich "bis 12:00" (aus der Etappenwahl) und
    # starteten mit demselben Ziel.
    #
    # Jetzt gilt: Hat der Aufrufer die Regel einer Etappe zugeordnet
    # (`target_uhr_override`), ist seine Zielzeit die Grundlage. Die
    # gelernte Zapfzeit korrigiert nur die Abend-Regel - dort liegt die
    # gelernte Zapfung (Befund 04.10.2026).
    if target_uhr_override is not None:
        ziel_uhr = float(_target_uhr)
        if learned_target_hour is not None and _name.endswith("-Abend"):
            ziel_uhr = float(learned_target_hour)
    else:
        ziel_uhr = (
            learned_target_hour
            if learned_target_hour is not None
            else float(calc_cfg.target_uhr)
        )

    # Etappenplan (Nutzerentscheidung 05.11.2026): mehrere Bedarfszeitpunkte,
    # Heizzeit je Etappe per Vorwaertssimulation des kalibrierten Speichers.
    # Die DRINGENDSTE Etappe liefert Zielzeit, Sollwert und Heizzeit; der
    # restliche Entscheidungsweg (PV-Fruehstart, Spaetest-Start, Netz-
    # Fallback, Notfall) bleibt unveraendert.
    # `etappen_filter` sorgt dafuer, dass jede Regel nur ihre eigene Etappe
    # bewertet (Mittag <-> "Mittag", Abend <-> "Abend").
    etappen_plan: List[Dict] = []
    etappen_hours: Optional[float] = None
    if getattr(calc_cfg, "etappen", None):
        etappen_ziel, etappen_soll, etappen_std, etappen_plan = _etappen_plan(
            calc_cfg, temp_dict, current_time, ziel_uhr,
            boiler_max_c=float(calc_cfg.tmax_c),
            etappen_filter=etappen_filter,
        )
        if etappen_ziel is not None:
            ziel_uhr = etappen_ziel
            etappen_hours = etappen_std
            if etappen_soll is not None:
                calc_cfg = calc_cfg.model_copy(
                    update={"solltemperatur_c": float(etappen_soll)}
                )

    # BUGFIX (22.10.2026): Zapfungs-Vorhaltung in die Zielzeit einrechnen.
    #
    # Nach einer Zapfung ist der Speicher unten kalt: Q Liter warmes Wasser
    # werden durch dieselbe Menge Kaltwasser ersetzt. Die Waermebilanz:
    #     dT = Q * (T_speicher - T_kalt) / (rho * c_p * V_speicher)
    # Damit das "unten" am Abend warm bleibt, muss der Speicher VOR der
    # Zapfung um genau diesen Betrag wärmer sein.
    #
    # Noch vor Zielzeit?
    if current_time >= ziel_uhr:
        # Nach Zielzeit ist der Fahrplan erfuellt - bewusst kein Notfall-Start
        # (Vertrag: `test_calcstart_zielzeit.py`; die zu frueh gelernte Zielzeit
        # darf keine Dauerstarts erzwingen). Liegt der Speicher danach trotzdem
        # deutlich unter Soll, heizt die Abweichungs-Regel (Prio 47) - seit dem
        # 18.10.2026 auch dann, wenn oben noch warm ist (Schichtungs-Start).
        # Das Journal nennt diesen Weg jetzt ausdruecklich, damit "Nach
        # Zielzeit" nicht mehr wie eine Stoerung aussieht (Befund 18.10.2026:
        # unten fiel von 41 auf 22 C, waehrend die Regel nur "Nach Zielzeit"
        # meldete).
        defizit_unten = max(0.0, calc_cfg.solltemperatur_c - temp_unten)
        mindestdef = float(getattr(calc_cfg, "notfall_mindestdefizit_k", 0.0) or 0.0)
        hinweis = ""
        if mindestdef > 0.0 and defizit_unten >= mindestdef:
            hinweis = (
                f" | unten {defizit_unten:.1f}K unter "
                f"{calc_cfg.solltemperatur_c:.0f}C: Heizung laeuft ueber die "
                f"Abweichungs-Regel (PV/Batterie)"
            )
        result.grund = (
            f"CalcStart: Nach Zielzeit ({now_hour}:{now_minute:02d} > "
            f"{_uhr_text(ziel_uhr)} gelernt"
            + (f", konfiguriert {_target_uhr}:00" if learned_target_hour is not None else "")
            + ")"
            + hinweis
        )
        return result

    # BUGFIX (22.10.2026): Zapfungs-Vorhaltung als ZielTEMPERATUR.
    #
    # Die Reserve gehoert in den SOLLwert, nicht in die Zielzeit: der
    # Speicher muss VOR der Zapfung um diesen Betrag waermer sein, damit
    # "unten" danach warm bleibt. Eine Erhoehung der Zielzeit dagegen
    # verschiebt nur den Start um Minuten (gemessen: 6 K Vorhaltung ->
    # 6 Minuten frueherer Start) - der Effekt war damit praktisch null.
    #
    # Beispiel (Live-Fall 15.10.2026): 150 l aus einem 300-l-Speicher mit
    # 42 C, Kaltwasser 10 C -> der Speicher verliert ca. 5,7 K unten.
    # Mit 5,7 K Vorhaltung heizt die WP auf 47,7 C - nach der Zapfung
    # bleiben 42 C, "unten" ist warm.
    soll_effektiv_c = float(calc_cfg.solltemperatur_c) + max(
        0.0, float(zapf_vorhaltung_k)
    )
    # Der Hinweis fuer die Grundtexte: der Betreiber soll sehen, dass der
    # Sollwert wegen der erwarteten Zapfung angehoben wurde.
    vorhaltung_hinweis = ""
    if zapf_vorhaltung_k > 0.0:
        vorhaltung_hinweis = (
            f" [Vorhaltung +{zapf_vorhaltung_k:.1f}K fuer "
            f"{zapfmenge_l:.0f}l Zapfung -> Soll "
            f"{soll_effektiv_c:.1f}C statt {calc_cfg.solltemperatur_c:.1f}C]"
        )

    # Temperaturdifferenz berechnen
    diff_unten = max(0, soll_effektiv_c - temp_unten)
    diff_mitte = max(0, soll_effektiv_c - temp_mitte)
    diff_oben = (
        max(0, soll_effektiv_c - temp_oben) if temp_oben is not None else 0
    )
    diff_gesamt = diff_unten + diff_mitte + diff_oben

    if etappen_hours is not None:
        if etappen_hours <= 0.0:
            result.grund = (
                "CalcStart: alle Etappenziele erreicht (%d Etappen geprueft)"
                % len(etappen_plan)
            )
            return result
    elif diff_gesamt <= 0:
        result.grund = f"CalcStart: Soll {calc_cfg.solltemperatur_c}C bereits erreicht"
        return result

    # Gelernte Heizraten verwenden (falls vorhanden), sonst Config-Defaults
    heizrate_unten = (
        learned_heating_rate_unten
        if learned_heating_rate_unten is not None
        else calc_cfg.heizrate_unten_c_h
    )
    heizrate_gesamt = (
        learned_heating_rate_gesamt
        if learned_heating_rate_gesamt is not None
        else calc_cfg.heizrate_gesamt_c_h
    )
    # `ziel_uhr` wird weiter oben einmalig bestimmt (siehe dort) und hier
    # bewusst NICHT erneut zugewiesen - zwei getrennte Bestimmungen waren
    # die Ursache des Befunds vom 04.10.2026.

    # Benoetigte Heizzeit: mit Etappen aus der Vorwaertssimulation, sonst aus
    # den gelernten Raten (altes Verhalten mit einem Ziel).
    if etappen_hours is not None:
        hours_needed = float(etappen_hours)
    else:
        hours_needed = diff_unten / max(heizrate_unten, 0.1)
        hours_needed_mitte = diff_mitte / max(heizrate_gesamt, 0.1)
        hours_needed = max(hours_needed, hours_needed_mitte)

    # === Multisensor-Zapfungsdaten einbeziehen (Punkt 4) ===
    # Temperaturverlust aus erkannter Zapfung abziehen, damit der
    # CalcStart die tatsaechliche Startzeit korrigeriert und die
    # Zapf-Garantie waerert.
    usage_drop_gesamt_k = 0.0
    if recent_usage_events and etappen_hours is None:
        # Letzte Zapfung nehmen und Temperaturverlust als "bereits verlorene
        # Energie" interpretieren -> reduziert effektive Heizzeit
        # (die WP muss den Verlust nicht mehr kompensieren, er ist
        #  already passiert; der Schicht-Temperaturabfall muss aber
        #  innerhalb des Zeitfensters ausgeglichen werden, daher
        #  begrenzen wir den Effekt auf max. 50% des Abfalls)
        neueste = recent_usage_events[-1]
        drop = neueste.get("drop_gesamt_k", 0.0)
        usage_drop_gesamt_k = round(drop * 0.5, 2)
        if usage_drop_gesamt_k > 0:
            hours_needed = max(
                0.0, hours_needed - usage_drop_gesamt_k / max(heizrate_unten, 0.1)
            )
            # Nur einmal je Ereignis loggen. Die Identitaet besteht bewusst
            # NUR aus Zeitstempel und Drop, NICHT aus dem errechneten
            # 'hours_needed': dieser Wert schwankt mit der Temperatur und
            # wuerde die Zeile sonst erneut im 10-s-Takt ausloesen - genau
            # das war der Spam. Die Korrektur selbst bleibt unveraendert bei
            # jedem Lauf aktiv.
            _debug_once(
                "calcstart_nutzungsevent",
                f"CalcStart: Nutzungsevent {neueste.get('timestamp','')} "
                f"drop {drop:.1f}K -> Stundenbedarf -{usage_drop_gesamt_k:.2f}h "
                f"(neu {hours_needed:.2f}h)",
                identity=f"{neueste.get('timestamp','')}|{drop:.3f}",
            )

    time_left = ziel_uhr - current_time

    # === Saisonale + Prognose-basierte Puffer-Berechnung ===
    # Saison erkennen (0=Winter, 1=Sommer)
    # Wir nutzen now_hour/min + wissen nicht direkt den Monat, aber wir
    # kriegen ihn ueber das now datetime objekt... da wir nur now_hour haben,
    # nutzen wir die Config-Puffer direkt mit Prognose-Anpassung.

    # Basis-Puffer aus Config
    # Prognose-Anpassung: Heute viel PV erwartet? -> laenger warten.
    # fc_ratio kalibriert die Prognose am gemessenen Netzeinschuss der
    # Vergangenheit (Haus-spezifischer Langfehler des Forecast-Dienstes).
    prognose_eff = forecast_wh_qm
    kalibrier_label = ""
    if forecast_wh_qm is not None and fc_ratio != 1.0:
        prognose_eff = forecast_wh_qm * fc_ratio
        kalibrier_label = f", Kalibrierung x{fc_ratio:.2f}"
    pv_faktor = 1.0
    if prognose_eff is not None:
        # Schwellen aus der Config (CalculatedStartConfig). Vorher standen
        # hier fest 3000/1500/500 im Code - eine Aenderung an
        # `forecast.fc_schwelle_hoch_wh` wirkte auf CalcStart dadurch nicht.
        if prognose_eff >= float(getattr(calc_cfg, "fc_schwelle_sehr_sonnig_wh", 3000.0)):
            pv_faktor = 2.0
            pv_label = f"sehr sonnig{kalibrier_label}"
        elif prognose_eff >= float(getattr(calc_cfg, "fc_schwelle_sonnig_wh", 1500.0)):
            pv_faktor = 1.5
            pv_label = f"sonnig{kalibrier_label}"
        elif prognose_eff <= float(getattr(calc_cfg, "fc_schwelle_bewoelkt_wh", 500.0)):
            pv_faktor = 0.5
            pv_label = f"bewoelkt{kalibrier_label}"
        else:
            pv_faktor = 1.0
            pv_label = f"{prognose_eff:.0f} Wh/qm{kalibrier_label}"
    else:
        pv_label = "keine Prognose"

    # Verbrauchsbewusstsein: Gelernte Stunden mit wenig Netzeinschuss
    # (Mittags Kochen etc.) zaehlen nur zu 75% als Heizzeit -> frueherer
    # Start, damit waehrend des Tiefs pausiert werden kann.
    dip_h, dip_label = (
        _mittagstief_stunden(
            current_time, ziel_uhr, surplus_profile,
            # BUGFIX (21.10.2026): Die Schwelle ist konfigurierbar, weil
            # das Profil jetzt den HAUSVERBRAUCH misst (vorher die
            # Einspeisung). Der alte Wert 250 W war fuer Einspeisung
            # gedacht - als Verbrauchsschwelle lag er unter dem
            # Grundverbrauch und jede Stunde galt als Spitze.
            schwelle_w=float(getattr(calc_cfg, "mittagstief_schwelle_w", 800.0)),
        )
        if surplus_profile
        else (0.0, "")
    )
    if dip_h:
        hours_needed = hours_needed + dip_h * 0.75

    buffer_hours = time_left - hours_needed
    effektiver_puffer = buffer_hours * pv_faktor

    quelle_ok, quelle_grund = _energiequelle_mit_grund(
        feedin_watt,
        soc,
        getattr(calc_cfg, "pv_einspeisung_min_watt", 50.0),
        getattr(calc_cfg, "soc_min_prozent", 90.0),
        getattr(calc_cfg, "max_netzbezug_watt", -50.0),
        pv_acpower=pv_acpower,
        battery_power=battery_power,
        battery_min_watt=getattr(calc_cfg, "batterie_entladung_min_watt", 50.0),
        solar_stale=solar_stale,
        pv_erlaubt=pv_quelle_erlaubt,
    )

    # Strukturierte Startplanung fuer API/WebApp/Diagnose. Die menschliche
    # Grundmeldung bleibt fuer Logs erhalten; die WebApp muss die Uhrzeit
    # nicht aus Freitext erraten.
    ideal_start_h = ziel_uhr - hours_needed - max(effektiver_puffer, 0.0)
    spaetester_start_h = ziel_uhr - hours_needed - float(
        getattr(calc_cfg, "spaetstart_puffer_h", 0.5)
    )
    # Ohne freigegebene Quelle ist der spaeteste Start der frueheste
    # tatsaechlich moegliche Start. Mit Quelle darf der Idealpuffer genutzt werden.
    geplanter_start_h = ideal_start_h if quelle_ok else spaetester_start_h
    result.regel_dict = {
        "planned_start_hour": round(max(current_time, geplanter_start_h), 2),
        "spaetester_start_hour": round(spaetester_start_h, 2),
        "target_hour": round(ziel_uhr, 2),
        "hours_needed": round(hours_needed, 2),
        "time_left_hours": round(time_left, 2),
        "effective_buffer_hours": round(effektiver_puffer, 2),
        # BUGFIX (22.10.2026): Die Vorhaltung sichtbar machen - der
        # Betreiber soll sehen, dass der Sollwert wegen der erwarteten
        # Zapfung angehoben wurde.
        "zapf_vorhaltung_k": round(max(0.0, float(zapf_vorhaltung_k)), 2),
        "zapfmenge_l": round(max(0.0, float(zapfmenge_l)), 1),
        "soll_effektiv_c": round(soll_effektiv_c, 1),
    }
    if etappen_plan:
        # Der ganze Plan (alle Etappen mit Restpuffer) gehoert in die
        # Diagnose: nur so ist nachvollziehbar, WARUM jetzt geheizt wird.
        result.regel_dict["etappen"] = etappen_plan

    # Netz-Start mit REDUZIERTEM Soll (Nutzerwunsch 28.09.2026).
    # Steht bewusst VOR dem Zeitablauf-/Spätest-Start-Zweig: Ohne PV wird
    # nicht mehr auf eine Quelle gewartet, sondern nur noch bis
    # `netz_solltemperatur_c` vorgeheizt. Die Zapfgarantie bleibt damit
    # erhalten, der Netzstrom-Verbrauch faellt aber kleiner aus als beim
    # Start bis zum vollen Soll. 0 = deaktiviert (alte Warte-Logik).
    netz_soll = float(getattr(calc_cfg, "netz_solltemperatur_c", 0.0) or 0.0)
    if not quelle_ok and netz_soll > 0.0:
        min_defizit = float(getattr(calc_cfg, "netz_einschalten_ab_k", 1.0))
        # Bedarf NUR fuer das reduzierte Ziel (unten bzw. mitte, der kältere zählt).
        std_n = max(
            max(0.0, netz_soll - float(temp_unten)),
            max(0.0, netz_soll - float(temp_mitte)),
        )
        stunden_n = std_n / max(heizrate_unten, 0.1)
        if std_n < min_defizit:
            # Reduzierter Soll praktisch erreicht: Netzstrom für <1 K lohnt nicht.
            result.einschalten = None
            result.grund = (
                f"CalcStart: reduzierter Netz-Soll {netz_soll:.0f}C nahezu "
                f"erreicht (unten {temp_unten:.1f}C, Defizit {std_n:.1f}K < "
                f"{min_defizit:.1f}K) -> warte auf PV/Batterie ({quelle_grund})"
            )
            return result
        if stunden_n <= time_left:
            result.einschalten = True
            result.grund = (
                f"CalcStart: Netz-Start ohne PV auf reduziertem Soll "
                f"{netz_soll:.0f}C statt {calc_cfg.solltemperatur_c:.0f}C "
                f"(unten {temp_unten:.1f}C = {std_n:.1f}K, {stunden_n:.1f}h <= "
                f"{time_left:.1f}h Restzeit bis {_uhr_text(ziel_uhr)}) -> EIN "
                f"({quelle_grund})"
            )
            return result
        result.einschalten = None
        result.grund = (
            f"CalcStart: Netz-Start auf reduziertem Soll {netz_soll:.0f}C noch "
            f"zu frueh (brauche {stunden_n:.1f}h, habe {time_left:.1f}h bis "
            f"{_uhr_text(ziel_uhr)}); warte auf PV/Batterie ({quelle_grund})"
        )
        return result

    if buffer_hours < 0:
        # Befund 15.10.2026, 18:37: "ZU SPAET! Zeitablauf -> EIN (Notfall)" bei
        # oben 48,4 C, mitte 48,0 C - nur unten fehlten 0,44 K. Der Speicher war
        # zu zwei Dritteln auf Soll; der Notfall-Start haette nur einen Kurzlauf
        # gegen das Boiler-Maximum erzeugt. Notfall nur bei echtem Defizit.
        mindestdef = float(getattr(calc_cfg, "notfall_mindestdefizit_k", 0.0) or 0.0)
        if (
            mindestdef > 0.0
            and temp_oben is not None
            and temp_mitte is not None
            and temp_oben >= calc_cfg.solltemperatur_c
            and temp_mitte >= calc_cfg.solltemperatur_c
            and diff_unten < mindestdef
        ):
            result.einschalten = None
            result.grund = (
                f"CalcStart: Zeitablauf, aber Speicher oben/mitte auf Soll "
                f"({temp_oben:.1f}/{temp_mitte:.1f}C) und unten nur "
                f"{diff_unten:.1f}K unter Ziel -> kein Notfall-Start"
            )
            return result
        # Bereits ueber Zielzeit oder zu spaet -> sofort heizen.
        # Netzfallback ist nur erlaubt, wenn explizit aktiviert und ab der
        # konfigurierten Uhrzeit freigegeben; sonst bleibt die Zapf-Garantie
        # sichtbar, aber die Regel wartet auf eine freigegebene Quelle.
        notfall_grenze = float(getattr(calc_cfg, "notfall_unten_c", 32.0))
        netzfallback_ok = (
            bool(getattr(calc_cfg, "netz_fallback_erlaubt", False))
            and now_hour >= int(getattr(calc_cfg, "netz_fallback_ab_uhr", 12))
        )
        if not quelle_ok and not netzfallback_ok and temp_unten > notfall_grenze:
            result.einschalten = None
            result.grund = (
                f"CalcStart: ZU SPAET ({time_left:.1f}h < {hours_needed:.1f}h), "
                f"aber Netzfallback ist deaktiviert; warte auf PV/Batterie "
                f"({quelle_grund}){vorhaltung_hinweis}"
            )
            return result
        result.einschalten = True
        result.grund = (
            f"CalcStart: ZU SPAET! Zeitablauf ({time_left:.1f}h bis "
            f"{_uhr_text(ziel_uhr)} < {hours_needed:.1f}h) -> EIN (Notfall)"
        )
        return result

    if quelle_ok and effektiver_puffer < 0.5:
        # Weniger als 30min effektiven Puffer + guenstige Quelle -> heizen
        result.einschalten = True
        result.grund = (
            f"CalcStart: Nur {effektiver_puffer:.1f}h Puffer (PV={pv_label}, "
            f"brauche {hours_needed:.1f}h bis {_uhr_text(ziel_uhr)}) -> EIN "
            f"[{quelle_grund}]{vorhaltung_hinweis}"
        )
        return result

    # Spaetest-Start-Schwelle: Der Puffer ist der Abstand zum berechneten
    # Spaetest-Start. Nutzerfrage 18.10.2026: Im Winter faellt dieser "letzte
    # Moment" hinter den Sonnenuntergang (gelernte Zielzeit 18:26, Vorlauf
    # 0,3 h -> 17:36; Sonnenuntergang Anfang Dezember ~16:00) - der Lauf waere
    # dann nicht mehr aus PV gedeckt. Deshalb wird die Schwelle auf das Ende
    # des PV-FENSTERS vorgezogen (letzte Prognose-Stunde ueber der Schwelle,
    # abzueglich Reserve). Rechnung in Puffer-Schreibweise:
    #   Start bei  t = Fensterende - Reserve
    #   Puffer(t) = (ziel - t) - hours_needed   <=   S
    #   -> S = ziel - hours_needed - (Fensterende - Reserve)
    # Nur wenn das frueher greift als der konfigurierte Puffer (max), und nur
    # bei aktiver Prognose. Ohne Stundenprognose bleibt alles unveraendert.
    puffer_schwelle = float(getattr(calc_cfg, "spaetstart_puffer_h", 0.5))
    if pv_fenster_stunde is not None and pv_fenster_aktiv:
        t_start_fenster = (
            float(pv_fenster_stunde) + 1.0 - max(float(pv_fenster_reserve_h), 0.0)
        )
        puffer_schwelle = max(
            puffer_schwelle, ziel_uhr - hours_needed - t_start_fenster
        )

    if buffer_hours <= puffer_schwelle:
        # Errechneter SPAETEST-START: Netzfallback nur explizit und zeitlich
        # freigegeben; sonst wartet die Regel auf PV/Batterie.
        netzfallback_ok = (
            bool(getattr(calc_cfg, "netz_fallback_erlaubt", False))
            and now_hour >= int(getattr(calc_cfg, "netz_fallback_ab_uhr", 12))
        )
        if not quelle_ok and not netzfallback_ok:
            result.einschalten = None
            result.grund = (
                f"CalcStart: SPAETEST-START ({buffer_hours:.1f}h Restpuffer), "
                f"aber Netzfallback ist deaktiviert; warte auf PV/Batterie "
                f"({quelle_grund})"
            )
            return result
        result.einschalten = True
        dip_txt = f" | Mittagstief {dip_label}" if dip_h else ""
        result.grund = (
            f"CalcStart: SPAETEST-START ({buffer_hours:.1f}h Restpuffer, "
            f"brauche {hours_needed:.1f}h bis {_uhr_text(ziel_uhr)}{dip_txt}) -> EIN "
            f"(Zapf-Garantie; Quelle: {quelle_grund})"
        )
        return result

    # Genug Puffer -> warten; ohne Quelle explizit benennen, worauf gewartet wird
    if not quelle_ok:
        dip_txt = f" | Mittagstief {dip_label}" if dip_h else ""
        result.einschalten = None
        result.grund = (
            f"CalcStart: {effektiver_puffer:.1f}h Puffer reicht, warte auf "
            f"PV/Batterie ({quelle_grund}; brauche {hours_needed:.1f}h bis "
            f"{_uhr_text(ziel_uhr)}{dip_txt}){vorhaltung_hinweis}"
        )
        return result
    result.einschalten = None
    dip_txt = f" | Mittagstief {dip_label}" if dip_h else ""
    result.grund = (
        f"CalcStart: {effektiver_puffer:.1f}h Puffer reicht (PV={pv_label}, "
        f"brauche {hours_needed:.1f}h bis {_uhr_text(ziel_uhr)}{dip_txt}) -> warte auf PV"
        + (f" (Soll {soll_effektiv_c:.1f}C statt {calc_cfg.solltemperatur_c:.1f}C "
           f"wegen {zapfmenge_l:.0f}l Zapfung)" if zapf_vorhaltung_k > 0.0 else "")
    )
    return result


