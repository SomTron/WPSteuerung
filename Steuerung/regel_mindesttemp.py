"""MindestTemp-Regel (Mindest-Temperatur-Garantien).

Ausgelagert aus priority_control.py. Re-Export in priority_control.py.
"""
from typing import Optional, Dict, List, Tuple

from rule_types import RegelErgebnis
from json_config import MindestTempConfig
from regel_helpers import _is_nachtsperre, _is_zeitfenster_active, _parse_sensor

def evaluate_mindesttemp(
    mindest_cfg: MindestTempConfig,
    temp_dict: Dict[str, Optional[float]],
    now_hour: int,
    nachtsperre_start: int,
    nachtsperre_ende: int,
    learned_evening_window: Optional[Tuple[float, float]] = None,
    learned_morning_window: Optional[Tuple[float, float]] = None,
) -> List[RegelErgebnis]:
    """Mindest-Temperatur-Garantien pro Fuehler und Zeitfenster.

    Zweck: Der Boiler darf zu definierten Zeiten nicht zu kalt sein
    (z.B. oben mittags >= 40C, mitte am Abend >= 40C zum Duschen).
    Innerhalb des Fensters gilt die Garantie AUCH waehrend der Nachtsperre -
    genau dafuer ist sie da. Ausnahme: Eintraege mit
    nachtsperre_ueberschreiben=False feuern NICHT in der Nachtsperre; ihre
    Garantie endet mit dem Sperren-Beginn (kein Nacht-Heizen ohne PV).
    Ausserhalb der Fenster entscheidet wie ueblich das Spar-Prioritaetensystem.
    """
    ergebnisse = []
    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)

    for eintrag in mindest_cfg.eintraege:
        name = f"MinTemp-{eintrag.name}"
        result = RegelErgebnis(
            name=name,
            prioritaet=mindest_cfg.prioritaet,
            aktiv=False,
        )

        if not mindest_cfg.aktiv:
            result.grund = "MindestTemp-Regel inaktiv"
            ergebnisse.append(result)
            continue
        # Zeitfenster: statisch aus Config oder dynamisch gelernt?
        start_uhr = eintrag.start_uhr
        ende_uhr = eintrag.ende_uhr
        fenster_hinweis = ""
        # Passendes gelerntes Fenster: Morgen-Eintraege nutzen NUR das
        # Morgen-Fenster, Abend-Eintraege NUR das Abend-Fenster.
        # Befund 07.10.2026: Vorher fiel ein Morgen-Eintrag ohne gelerntes
        # Morgen-Fenster (None) auf das ABEND-Fenster zurueck und bekam
        # dessen Stundenbereich (z.B. 16-20h) mit seinen eigenen
        # Klemmgrenzen gemischt -> aus der 5-8-Uhr-Garantie wurde ein
        # 16-17-Uhr-Fenster und die morgendliche Garantie war still tot.
        # Ist das passende Fenster None, bleibt das konfigurierte Fenster
        # unveraendert (kein Lernen, kein Fallback aufs falsche Fenster).
        lern_fenster = (
            learned_morning_window
            if eintrag.start_uhr < 12
            else learned_evening_window
        )
        if eintrag.fenster_aus_lernen and lern_fenster is not None:
            l_start, l_ende = lern_fenster
            # Gelerntes Fenster mit Sicherheitsgrenzen klemmen:
            # max. 2h frueher Start, max. 1h spaeteres Ende als konfiguriert.
            import math

            start_uhr = max(eintrag.start_uhr - 2, int(math.floor(l_start)))
            ende_uhr = min(eintrag.ende_uhr + 1, int(math.ceil(l_ende)))
            ende_uhr = max(ende_uhr, start_uhr + 1)
            fenster_hinweis = (
                f" [gelernt {l_start:.1f}-{l_ende:.1f}h -> {start_uhr}-{ende_uhr} Uhr]"
            )

        if not _is_zeitfenster_active(now_hour, start_uhr, ende_uhr):
            result.grund = (
                f"{eintrag.name}: auserhalb Fenster {start_uhr}-{ende_uhr} Uhr"
                f"{fenster_hinweis}"
            )
            ergebnisse.append(result)
            continue

        # Nachtsperre respektieren? Garantien duerfen die Sperre normalerweise
        # ueberschreiben - genau dafuer sind sie da. Eintraege mit
        # nachtsperre_ueberschreiben=False bleiben innerhalb der Sperre aber
        # stumm: Die Garantie gilt nur BIS zum Sperren-Beginn (z.B. abends
        # warm genug zum Duschen), danach wird nachts nicht mehr nachgeheizt
        # (nachts gibt es kein PV -> sonst Netzstrom).
        if nachtsperre and not eintrag.nachtsperre_ueberschreiben:
            result.grund = (
                f"{eintrag.name}: Nachtsperre aktiv ({nachtsperre_start}-{nachtsperre_ende} Uhr) "
                f"- Garantie ueberschreibt nicht{fenster_hinweis}"
            )
            ergebnisse.append(result)
            continue

        temp = _parse_sensor(temp_dict, eintrag.temperaturfuehler)
        if temp is None:
            result.grund = (
                f"{eintrag.name}: Sensor '{eintrag.temperaturfuehler}' nicht verfuegbar"
            )
            ergebnisse.append(result)
            continue

        result.aktiv = True
        aus_schwelle = eintrag.min_temp_c + eintrag.hysterese_k

        # WICHTIG: Diese Regel ist rein ADDITIV - sie kann nur EINSCHALTEN
        # (Garantieverletzung), aber niemals blockieren. Ein explizites AUS
        # wuerde mit ihrer hohen Prioritaet Heizwuensche anderer Regeln
        # (PV/Abweichung/CalcStart) ungewollt abschneiden. Ist die Garantie
        # erfuellt, tritt die Regel therefore stumm zurueck (einschalten=None).
        # Die Ausschaltung nach einem Garantie-Einschalten uebernimmt der
        # normale Setpoint (min_temp_c + hysterese_k via _extract_ausschaltpunkt).

        # EINSCHALTEN: unter der Mindesttemperatur (garantiert auch in Nachtsperre)
        if temp < eintrag.min_temp_c:
            result.einschalten = True
            hinweis = " [Nachtsperre ueberschrieben]" if nachtsperre else ""
            result.grund = (
                f"MindestTemp: {eintrag.temperaturfuehler} {temp:.1f}C < "
                f"{eintrag.min_temp_c}C -> EIN{hinweis}{fenster_hinweis}"
            )
            ergebnisse.append(result)
            continue

        # Hysterese-Zone bzw. Garantie erfuellt -> keine Aktion
        if temp >= aus_schwelle:
            grund_text = (
                f"MindestTemp: {eintrag.temperaturfuehler} {temp:.1f}C >= "
                f"{aus_schwelle:.1f}C ({eintrag.min_temp_c}C+{eintrag.hysterese_k}K) "
                f"-> Garantie erfuellt"
            )
        else:
            grund_text = (
                f"MindestTemp: {eintrag.temperaturfuehler} {temp:.1f}C in Hysterese "
                f"({eintrag.min_temp_c}-{aus_schwelle:.1f}C)"
            )
        result.einschalten = None
        result.grund = grund_text
        ergebnisse.append(result)

    return ergebnisse


# evaluate_einspeisung ist in regel_einspeisung.py ausgelagert.
from regel_einspeisung import evaluate_einspeisung  # noqa: E402, F401

# evaluate_batterie ist in regel_batterie.py ausgelagert.
from regel_batterie import evaluate_batterie  # noqa: E402, F401

# evaluate_wochenende ist in regel_wochenende.py ausgelagert.
from regel_wochenende import evaluate_wochenende  # noqa: E402, F401

# evaluate_notfallschutz ist in regel_notfallschutz.py ausgelagert.
from regel_notfallschutz import evaluate_notfallschutz  # noqa: E402, F401
from regel_komfort import evaluate_komfort  # noqa: E402, F401
from regel_zeitfenster import evaluate_zeitfenster  # noqa: E402, F401

