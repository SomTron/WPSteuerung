"""Ausgelagert aus priority_control.py. Re-Export in priority_control.py.
"""
from typing import Optional, Dict
from rule_types import RegelErgebnis
from json_config import NotfallschutzConfig
from regel_helpers import _parse_sensor

def evaluate_notfallschutz(
    nf_cfg: NotfallschutzConfig,
    temp_dict: Dict[str, Optional[float]],
    kompressor_ein: bool = False,
    legionellen_aktiv: bool = False,
    legionellen_max_c: Optional[float] = None,
    legionellen_angekuendigt: bool = False,
    notfall_aktiv: bool = False,
    now_hour: Optional[int] = None,
    nacht_ende: int = 8,
) -> RegelErgebnis:
    """Notfallschutz (Prio 110): Reiner Schutzleiter fuer die
    Brauchwasser-Mindesttemperatur.

    Aus dem alten Komfort-Notfall ausgekoppelt. Greift ohne weitere Bedin-
    gungen vor ALLEN Sperren (Wochenende, Nachtsperre): Sinkt die Nutz-
    Wassertemperatur unter einschalten_bei_c, wird eingeschaltet. Im
    Normalbetrieb ist die Regel STUMM (einschalten=None) - sie blockt
    dadurch niemals andere Heizwuensche.

    `notfall_aktiv` (aus dem State) unterscheidet "der Kompressor laeuft
    ueberhaupt" von "der Kompressor laeuft WEIL der Notfallschutz ihn
    gestartet hat". Nur im zweiten Fall duerfen die Hysterese-Zweige
    einschalten=False liefern. Sonst wuerde der Schutzleiter bei warmem
    `oben` jeden Lauf einer beliebigen anderen Regel (Prio 60/47/78) ab
    Prio 110 abwuergen - genau das erzeugte das Flattern im Log vom
    28.09. (oben 57.8C -> "Hysterese -> AUS" waehrend ein Komfort-Start
    anlag).
    """
    result = RegelErgebnis(
        name="Notfallschutz",
        prioritaet=nf_cfg.prioritaet,
        aktiv=nf_cfg.aktiv,
    )
    if not nf_cfg.aktiv:
        result.grund = "Notfallschutz inaktiv"
        return result

    # "auto" ist bewusst eine Prioritätsstrategie: der erste gültige Fühler
    # entscheidet (oben -> mittig -> unten). "alle" nutzt dagegen den kältesten.
    strategie = getattr(nf_cfg, "temperaturfuehler", "auto")
    if strategie == "alle":
        kandidaten = []
        for name in ("oben", "mittig", "unten"):
            wert = _parse_sensor(temp_dict, name)
            if wert is not None:
                kandidaten.append((wert, name))
        if not kandidaten:
            result.aktiv = False
            result.grund = "Keine Sensordaten fuer Notfallschutz verfuegbar"
            return result
        temp, sensor = min(kandidaten, key=lambda item: item[0])
    elif strategie == "auto":
        temp = None
        sensor = ""
        for name in ("oben", "mittig", "unten"):
            temp = _parse_sensor(temp_dict, name)
            if temp is not None:
                sensor = name
                break
    else:
        temp = _parse_sensor(temp_dict, strategie)
        sensor = strategie
    if temp is None:
        result.aktiv = False
        result.grund = f"Sensor '{sensor or strategie}' fuer Notfallschutz nicht verfuegbar"
        return result

    # Tages-Cutoff (Nutzerwunsch 28.09.2026: der Notfall laeuft spaetestens
    # bis 22:00). Zwei Faelle, bewusst getrennt:
    #   * EIGENER Notfall-Lauf -> beenden (hartes Ausschalten).
    #   * fremder Lauf          -> still bleiben. Sonst wuerde der
    #     Notfallschutz (Prio 110) mit seinem AUS auch PV-/Boiler-Laeufe
    #     abwuergen, die gar nicht von ihm stammen.
    # Eine aktive LEGIONELLENfahrt ist ausgenommen: sie zielt auf 60 C,
    # dauert bis zu `max_duration_hours` und darf nicht bei 22:00
    # abgeschnitten werden - sonst erreichte die Prophylaxe ihr Ziel nie.
    # NACHT: "spaetestens 22:00" heisst, dass 22:00-07:59 gesperrt ist.
    # Der Vergleich darf daher NICHT nur `hour >= 22` sein - 03:00 waere
    # sonst wieder erlaubt. Gesperrt ist [cutoff, 24) plus
    # [0, Nachtsperre-Ende).
    cutoff = getattr(nf_cfg, "bis_uhr", None)
    if cutoff is not None and now_hour is not None and int(cutoff) > 0:
        _cut = int(cutoff)
        # Nachtsperren-Ende kommt aus der Config (bisher hardcodiert 8 -
        # bei abweichender Nachtsperre passte der Cutoff sonst nicht mehr).
        _nacht_ende = max(int(nacht_ende), 0)
        if _cut > _nacht_ende:
            _gesperrt = int(now_hour) >= _cut or int(now_hour) < _nacht_ende
        else:
            # Unsinnige Lage (Cutoff <= Nachtende) -> nur im Tagfenster.
            _gesperrt = _nacht_ende <= int(now_hour) < _cut
        if _gesperrt and not legionellen_aktiv:
            if notfall_aktiv and kompressor_ein:
                result.einschalten = False
                result.grund = (
                    f"Notfallschutz: Tages-Cutoff {int(cutoff):02d}:00 erreicht "
                    f"({sensor} {temp:.1f}C) -> AUS"
                )
                return result
            # AB HIER: kein laufender eigener Notfall, aber der Start ist
            # noch offen. Der Cutoff ist ein LAUFZEIT-Wunsch (Nutzerwunsch
            # 28.09.: "der Notfall laeuft spaetestens bis 22:00"), KEIN
            # Startverbot fuer Untertemperatur.
            #
            # Befund aus der Regelanalyse 04.10.2026: hier stand vorher
            # bedingungslos `einschalten = None`. Damit fiel der Schutzleiter
            # aus `aktive_regeln` heraus (`if e.aktiv and e.einschalten is
            # None` wird gefiltert) und Wochenende (Prio 100, einschalten=
            # False) gewann die Wahl. Nachgerechnet: Sonntag 07:00, Speicher
            # 18 C, bis_uhr=22 -> "Start gesperrt -> AUS" bei 18 Grad. Der
            # Prio-Wert 110 nuetzte nichts, weil die Regel nicht zur Wahl
            # stand.
            #
            # Untertemperatur ist ein Sicherheitsfall, kein Komfortwunsch:
            # deshalb startet der Notfallschutz auch nach dem Cutoff. Der
            # laufende Notfall endet weiterhin puenktlich am Cutoff.
            if temp <= nf_cfg.einschalten_bei_c:
                result.einschalten = True
                result.grund = (
                    f"NOTFALLSCHUTZ: {sensor} {temp:.1f}C <= "
                    f"{nf_cfg.einschalten_bei_c:.1f}C -> EIN "
                    f"(nach Tages-Cutoff {int(cutoff):02d}:00, aber "
                    "Untertemperatur hat Vorrang)"
                )
                return result
            result.einschalten = None
            result.grund = (
                f"Notfallschutz: nur bis {int(cutoff):02d}:00 aktiv "
                f"({sensor} {temp:.1f}C) -> kein Notfallstart"
            )
            return result

    if kompressor_ein and legionellen_aktiv and temp > nf_cfg.einschalten_bei_c:
        result.einschalten = None
        result.grund = (
            f"Notfallschutz: {sensor} {temp:.1f}C > {nf_cfg.einschalten_bei_c:.1f}C; "
            "aktive Legionellenfahrt bleibt bis zum konfigurierten Limit wirksam"
        )
        return result
    # ANGEKUENDIGTE Legionellenfahrt (Plan betrifft heute / Nachversuch nach
    # Abbruch): Der Schutzleiter darf einen faelligen Prophylaxe-Start NICHT
    # wieder abwuergen. Genau das brach am 25.09.2026 zwei Anlaufversuche ab:
    # der Notfall lief noch (`notfall_aktiv`), die Legionellenfahrt war aber
    # noch nicht per Hardware-Bestaetigung als aktiv markiert - ihre 38-C-
    # Hysterese (Prio 110, also ueber Legionelle 90) killte den Start.
    if kompressor_ein and legionellen_angekuendigt and temp > nf_cfg.einschalten_bei_c:
        result.einschalten = None
        result.grund = (
            f"Notfallschutz: {sensor} {temp:.1f}C > {nf_cfg.einschalten_bei_c:.1f}C; "
            "angekuendigte Legionellenprophylaxe laeuft - kein Eingriff"
        )
        return result
    if temp <= nf_cfg.einschalten_bei_c:
        result.einschalten = True
        result.grund = (
            f"NOTFALLSCHUTZ: {sensor} {temp:.1f}C <= "
            f"{nf_cfg.einschalten_bei_c}C -> EIN"
        )
        return result
    # Abschaltgrenze des Notfallschutzes. Normalerweise die Komfort-
    # Hysterese (38 C). Waehrend einer Legionellenfahrt gilt das
    # konfigurierte Legionellen-Limit (65 C): die Regel hat Prio 110 und
    # wuerde die Prophylaxe sonst bei bereits erreichten 45-58 C abwuergen.
    # Befund aus dem Laufzeitlog 18.09.-01.10.2026: zwei Abbruchversuche
    # am 25.09. bei oben 45,7 C, weil die Hysterese bei 38 C griff.
    nf_ausschalten_c = float(nf_cfg.ausschalten_bei_c)
    if legionellen_aktiv and legionellen_max_c is not None:
        nf_ausschalten_c = max(nf_ausschalten_c, float(legionellen_max_c))
    if notfall_aktiv and kompressor_ein and temp >= nf_ausschalten_c:
        result.einschalten = False
        suffix = " (Legionellenfahrt)" if nf_ausschalten_c > nf_cfg.ausschalten_bei_c else ""
        result.grund = (
            f"Notfallschutz-Hysterese: {sensor} {temp:.1f}C >= "
            f"{nf_ausschalten_c:.1f}C -> AUS{suffix}"
        )
        return result
    if notfall_aktiv and kompressor_ein:
        result.einschalten = True
        result.grund = (
            f"Notfallschutz-Hysterese: {sensor} {temp:.1f}C zwischen "
            f"{nf_cfg.einschalten_bei_c}-{nf_ausschalten_c:.1f}C -> laeuft weiter"
        )
        return result
    if kompressor_ein:
        # Der Kompressor laeuft, aber NICHT ueber den Notfallschutz. Der
        # Schutzleiter hat dann nichts zu tun: insbesondere darf er den
        # fremden Lauf nicht beenden (Prio 110 wuerde sonst jede andere
        # Regel aushebeln, sobald `oben` ueber der Abschaltschwelle liegt).
        result.grund = (
            f"Notfallschutz: {sensor} {temp:.1f}C > {nf_cfg.einschalten_bei_c}C, "
            "fremder Lauf - kein Eingriff"
        )
        return result

    result.einschalten = None
    result.grund = (
        f"Notfallschutz: {sensor} {temp:.1f}C > {nf_cfg.einschalten_bei_c}C (stumm)"
    )
    return result


# evaluate_komfort ist in regel_komfort.py ausgelagert.
from regel_komfort import evaluate_komfort  # noqa: E402, F401

# evaluate_zeitfenster ist in regel_zeitfenster.py ausgelagert.
from regel_zeitfenster import evaluate_zeitfenster  # noqa: E402, F401

