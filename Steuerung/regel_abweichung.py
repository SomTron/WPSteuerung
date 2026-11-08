"""Abweichungs-Regel (Solltemperatur-Hysterese am unteren Fuehler).

Ausgelagert aus priority_control.py. Re-Export in priority_control.py.
"""
from typing import Optional, Dict

from rule_types import RegelErgebnis
from json_config import AbweichungConfig
from regel_helpers import _energiequelle_mit_grund, _energiequelle_ok, _fmt_pv, _forecast_effektiv_wh_qm, _is_nachtsperre, _parse_sensor, _sensor_oder_none

def evaluate_abweichung(
    abw: AbweichungConfig,
    temp_dict: Dict[str, Optional[float]],
    # BUGFIX (22.10.2026): Dieser Parameter wird hier NICHT gelesen.
    # Er stand nur in der Signatur und wurde deshalb als irrefuehrend
    # verkannt (er suggerierte, der Kompressor-Zustand fliesse in die
    # Hysterese ein - tut er aber nicht: die Aussage "Kompressor
    # laeuft weiter / bleibt aus" wird ueber `result.einschalten = None`
    # ausgedrueckt).
    #
    # Er bleibt an seiner Position, weil 9 Aufrufer ihn positional
    # uebergeben. Ein Entfernen oder Verschieben erzwingt die Umstellung
    # ALLER Aufrufer - bei einem ungenutzten Parameter ein Risiko ohne
    # Gegenwert. Die Umstellung gehoert in einen Schritt, der die
    # Regel tatsaechlich umbaut.
    kompressor_ein: bool,
    now_hour: int,
    nachtsperre_start: int,
    nachtsperre_ende: int,
    feedin_watt: float = 0.0,
    soc: Optional[float] = None,
    bademodus_aktiv: bool = False,
    forecast_today_wh_qm: Optional[float] = None,
    fc_ratio: float = 1.0,
    pv_acpower: Optional[float] = None,
    battery_power: Optional[float] = None,
    solar_stale: bool = False,
    notfall_einschalten_c: Optional[float] = None,
    pv_quelle_erlaubt: bool = True,
    # BUGFIX (22.10.2026): Stundenprognose (W) fuer den Rest-Ertrag-Check.
    forecast_hourly_wh: Optional[Dict[int, float]] = None,
    # Speichervolumen in Litern - fuer den Energiebedarf des Warmstarts.
    speicher_volumen_l: Optional[float] = None,
) -> RegelErgebnis:
    """
    Abweichungs-Regel: haelt den Boiler auf Solltemperatur (Komfort-Boden).
    
    Regelfuehler ist `abw.temperaturfuehler` (Deployment: "unten"): Der untere
    Fuehler ist der kalteste Punkt und der Fruehindikator fuer "Boiler
    entladen" (Zapfung bringt Kaltwasser unten ein). "oben" bleibt durch die
    Schichtung lange warm und waere als Regelfuehler blind dafuer.

    Schutz vor der Schichtungs-Falle ("oben heiss, unten kalt"):
    Ist `oben >= schichtung_min_oben_c` (Default 42 C), entscheidet
    `schichtung_erlaube_start`:
      * false (Deployment) -> es wird NICHT geheizt, die Regel wartet
      * true  -> Warmstart mit Deckel: oben darf max. `schichtung_max_steig_k`
                 (Default 1 K) steigen
    ACHTUNG: Dieser Zweig liegt VOR dem Quellen-Gate - bei `true` kann also
    auch ohne PV/Batterie geheizt werden. Das ist aber nur zulaessig, wenn der
    Speicher INSGESAMT zu kalt ist: `schichtung_netz_max_oben_c` (Default
    45 C) begrenzt den Netz-Warmstart. Liegt `oben` darueber, ist das Wasser
    am Hahn bereits zapfwarm und der kalte Boden soll mit PV nachgeheizt
    werden, nicht mit Netzstrom (Befund Entscheidungslog 30.09.2026).

    Danach Quellen-Gate (nur wenn der Schichtungs-Zweig nicht gegriffen hat):
      * `quelle_warten` (Default true): EIN nur mit PV-Einspeisung oder voller
        Batterie, sonst wartet die Regel (stumm, kein AUS)
      * Tiefschutz: unter `solltemperatur_c - netz_notfall_offset_k` ist
        Netzstrom erlaubt, damit der Boiler nicht durchkuehlt
      * `pv_warten_*`: bei guter Tagesprognose vor `pv_warten_bis_uhr` auf die
        Sonne warten; unter `pv_warten_unten_min_c` gilt "echt kalt" -> sofort

    AUS erfolgt ueber den Regelfuehler: `soll - temp <= ausschalten_bei_abweichung_k`
    """
    result = RegelErgebnis(name="Abweichung", prioritaet=abw.prioritaet, aktiv=True)

    temp = _sensor_oder_none(temp_dict, abw.temperaturfuehler, result)
    if temp is None:
        return result

    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)
    abweichung = abw.solltemperatur_c - temp  # Positiv = zu kalt, Negativ = zu warm

    # Ausschalten: Ziel erreicht oder ueberschritten (immer erlaubt, auch Nachts)
    if abweichung <= abw.ausschalten_bei_abweichung_k:
        result.einschalten = False
        result.grund = (
            f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
            f"{abweichung:.1f}K <= +{abw.ausschalten_bei_abweichung_k}K -> AUS"
        )
        return result

    # Bei Nachtsperre: Kein Einschalten (Blockierungsgrund anzeigen)
    # AUSNAHME Bademodus: Der Nutzer moechte warmes Wasser, Nachtsperre
    # darf dann nicht blockieren.
    if nachtsperre and not bademodus_aktiv:
        result.aktiv = False
        result.grund = (
            f"Nachtsperre (kein Einschalten, {abw.temperaturfuehler}={temp:.1f}C)"
        )
        return result

    # Einschalten: Zu kalt
    if abweichung >= abw.einschalten_bei_abweichung_k:
        # PV-Warten-Overlay (Empfehlung 3.3): Bei sehr guter Tagesprognose
        # morgens NICHT mit Netzstrom vorheizen, sondern auf die erwartete
        # PV-Sonne warten. Steht vor dem Schichtungs-Check, damit auch der
        # "oben warm, unten kalt"-Warmstart nicht aus dem Netz bedient wird.
        # Schutz: Unter pv_warten_unten_min_c (echt kalt) wird sofort geheizt,
        # ab pv_warten_bis_uhr spielt die Regel wieder normal; ist die Sonne
        # zum Wartezeitpunkt schon aktiv, wird normal entschieden.
        # BUGFIX (22.10.2026): Das PV-Warten prueft den TAGES-Forecast -
        # nicht, wie viel ERTRAG bis zur Wartezeitgrenze noch kommt.
        #
        # Das war falsch: um 11:00 ist der meiste Tagesertrag schon
        # realisiert. Die Regel wartet dann trotzdem bis 12:00 auf die
        # Sonne, obwohl der Rest-Ertrag den Bedarf nicht mehr deckt. Der
        # Boiler bleibt zu kalt, und die Regel tut so, als haette sie
        # richtig gewartet.
        #
        # Neu: Wenn die Stundenprognose UND das Speichervolumen
        # bekannt sind, wird der Rest-Ertrag bis `pv_warten_bis_uhr`
        # gegen den Aufheizbedarf geprueft. Passt er nicht, wird
        # NORMAL entschieden (also aus dem Netz geheizt, weil das
        # Heizen auf PV die Wartezeit nur verbrannt).
        #
        # Ohne Stundenprognose oder Volumen bleibt es beim alten
        # Verhalten (fail-open: lieber zu spaet als zu frue).
        rest_ertrag_ok = True
        rest_ertrag_wh = None
        bedarf_wh = None
        if forecast_hourly_wh and speicher_volumen_l:
            try:
                vol_l = float(speicher_volumen_l)
                # Bedarf: (Soll - Ist) x Volumen x 1,163 Wh/(l*K)
                bedarf_wh = max(
                    0.0,
                    (float(abw.solltemperatur_c) - float(temp))
                    * vol_l * 1.163,
                )
                # Rest-Ertrag von jetzt bis zur Wartezeitgrenze
                grenze = int(getattr(abw, "pv_warten_bis_uhr", 12))
                rest_wh = 0.0
                for stunde, watt in forecast_hourly_wh.items():
                    try:
                        h = int(stunde)
                        w = float(watt)
                    except (TypeError, ValueError):
                        continue
                    if now_hour <= h < grenze and w > 0:
                        # Die Stundenprognose ist eine LEISTUNG in W fuer
                        # die jeweilige Stunde. Fuer die Energie muss sie
                        # mit der Stundendauer multipliziert werden
                        # (1 h). Ohne das waeren die Wh um Faktor 3600
                        # zu klein - der Check wuerde immer "reicht nicht"
                        # sagen und die Regel nie warten.
                        rest_wh += w * 1.0
                rest_ertrag_wh = rest_wh
                # 30 % Reserve: der Ertrag ist eine Prognose.
                rest_ertrag_ok = rest_wh >= bedarf_wh * 1.3
            except (TypeError, ValueError, ZeroDivisionError):
                rest_ertrag_ok = True
                rest_ertrag_wh = None
                bedarf_wh = None

        if (
            getattr(abw, "pv_warten_aktiv", True)
            and now_hour < getattr(abw, "pv_warten_bis_uhr", 12)
            and temp >= getattr(abw, "pv_warten_unten_min_c", 20.0)
            and forecast_today_wh_qm is not None
            and _forecast_effektiv_wh_qm(forecast_today_wh_qm, fc_ratio)
            >= getattr(abw, "pv_warten_forecast_schwelle_wh_qm", 2500.0)
            and rest_ertrag_ok
            and not _energiequelle_ok(
                feedin_watt,
                soc,
                getattr(abw, "pv_einspeisung_min_watt", 50.0),
                getattr(abw, "soc_min_prozent", 90.0),
                getattr(abw, "max_netzbezug_watt", -50.0),
                pv_acpower=pv_acpower,
                battery_power=battery_power,
                battery_min_watt=getattr(abw, "batterie_entladung_min_watt", 50.0),
                solar_stale=solar_stale,
                pv_erlaubt=pv_quelle_erlaubt,
            )
        ):
            eff = _forecast_effektiv_wh_qm(forecast_today_wh_qm, fc_ratio)
            result.einschalten = None
            result.grund = (
                f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
                f"+{abweichung:.1f}K >= +{abw.einschalten_bei_abweichung_k}K, "
                f"heute gute PV-Prognose ({eff:.0f} Wh/m2, "
                f"Erzeugung {_fmt_pv(pv_acpower)}W, Einspeisung {feedin_watt:.0f}W) "
                f"-> warte auf PV (Netz ab {getattr(abw, 'pv_warten_bis_uhr', 12)}:00 "
                f"oder Notfall unter {getattr(abw, 'pv_warten_unten_min_c', 20.0):.0f}C)"
                # BUGFIX (22.10.2026): Der Rest-Ertrag wird mitgenannt, damit
                # der Betreiber sieht, WORAUF die Entscheidung beruht.
                + (
                    f", Rest-Ertrag bis {getattr(abw, 'pv_warten_bis_uhr', 12)}:00 "
                    f"{rest_ertrag_wh:.0f}Wh reicht fuer {bedarf_wh:.0f}Wh"
                    if rest_ertrag_wh is not None and bedarf_wh is not None
                    else ""
                )
            )
            return result
        # 2-Zonen-Schichtungs-Check: Wenn der konfigurierte Fuehler nicht "oben" ist
        # (z.B. unten/mittig), pruefe ob oben noch warm genug ist.
        if abw.temperaturfuehler != "oben" and abw.schichtung_min_oben_c > 0:
            temp_oben = _parse_sensor(temp_dict, "oben")
            if temp_oben is not None and temp_oben >= abw.schichtung_min_oben_c:
                erlaube = getattr(abw, "schichtung_erlaube_start", False)
                steig = max(getattr(abw, "schichtung_max_steig_k", 1.0), 0.1)
                if erlaube:
                    quelle_ok, quelle_grund = _energiequelle_mit_grund(
                        feedin_watt,
                        soc,
                        getattr(abw, "pv_einspeisung_min_watt", 50.0),
                        getattr(abw, "soc_min_prozent", 90.0),
                        getattr(abw, "max_netzbezug_watt", -50.0),
                        pv_acpower=pv_acpower,
                        battery_power=battery_power,
                        battery_min_watt=getattr(abw, "batterie_entladung_min_watt", 50.0),
                        solar_stale=solar_stale,
                        pv_erlaubt=pv_quelle_erlaubt,
                    )
                    netz_erlaubt = getattr(
                        abw, "schichtung_netz_fallback_erlaubt", False
                    )
                    if not quelle_ok and not netz_erlaubt:
                        tief_grenze = abw.solltemperatur_c - max(
                            getattr(abw, "netz_notfall_offset_k", 8.0), 0.0
                        )
                        if temp > tief_grenze:
                            result.einschalten = None
                            result.grund = (
                                f"oben {temp_oben:.1f}C warm, Schichtungs-Start verlangt "
                                f"PV/Batterie: {quelle_grund}"
                            )
                            return result
                        # Tiefenschutz bleibt auch im Schichtungs-Warmstart
                        # fail-safe erhalten; nur unterhalb der Notfallgrenze.
                        #
                        # NEU (Befund Entscheidungslog 18.09.-01.10.2026):
                        # Der Tiefenschutz allein war zu weit. Am 30.09. lief
                        # ein Netz-Vorheizlauf 06:00-08:37 bei unten 20,8 C
                        # (also 21 K unter Soll), waehrend OBEN 46,7 C stand -
                        # der Speicher war also zapfwarm. Um 10:09 kam die PV
                        # mit 2,5 kW und hatte diese Arbeit um 12:45 erledigt.
                        # Deshalb: der Netz-Warmstart ist nur noch zulaessig,
                        # wenn der Speicher insgesamt zu kalt ist
                        # (oben < schichtung_netz_max_oben_c). Darueber wird
                        # gewartet - genau das war die dokumentierte Absicht der
                        # Regel ("morgens NICHT mit Netzstrom vorheizen"), die
                        # im Log nie gegriffen hat.
                        netz_max_oben = float(
                            getattr(abw, "schichtung_netz_max_oben_c", 0.0) or 0.0
                        )
                        if netz_max_oben > 0.0 and temp_oben >= netz_max_oben:
                            result.einschalten = None
                            result.set_reason_code("waiting_source")
                            result.grund = (
                                f"Soll {abw.solltemperatur_c}C - "
                                f"{abw.temperaturfuehler} {temp:.1f}C = "
                                f"+{abweichung:.1f}K, aber oben {temp_oben:.1f}C "
                                f">= {netz_max_oben:.1f}C: Speicher ist zapfwarm - "
                                "warte auf PV/Batterie statt Netz-Vorheizung"
                            )
                            return result
                        result.einschalten = True
                        result.regel_dict = {
                            "schichtung_oben_max": temp_oben + steig,
                            "schichtung_oben_start": temp_oben,
                            "schichtung_max_steig_k": steig,
                            "schichtung_netz_fallback": True,
                        }
                        result.grund = (
                            f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
                            f"+{abweichung:.1f}K >= +{abw.einschalten_bei_abweichung_k}K, "
                            f"oben {temp_oben:.1f}C warm, Tiefenschutz <= {tief_grenze:.1f}C "
                            f"-> EIN mit Netz; Obergrenze oben {temp_oben + steig:.1f}C"
                        )
                        return result
                    # Netzfallback ist nur durch die explizite Option aktiv.
                    result.einschalten = True
                    result.regel_dict = {
                        "schichtung_oben_max": temp_oben + steig,
                        "schichtung_oben_start": temp_oben,
                        "schichtung_max_steig_k": steig,
                        "schichtung_netz_fallback": not quelle_ok,
                    }
                    result.grund = (
                        f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
                        f"+{abweichung:.1f}K >= +{abw.einschalten_bei_abweichung_k}K, "
                        f"oben {temp_oben:.1f}C warm, Schichtungs-Start "
                        f"({quelle_grund if quelle_ok else 'Netzfallback explizit erlaubt'}) "
                        f"-> EIN; Obergrenze oben {temp_oben + steig:.1f}C"
                    )
                    return result
                else:
                    # Tiefenschutz gilt AUCH wenn der Schichtungs-Warmstart
                    # verboten ist: Faellt der Fuehler auf/unter die Notfall-
                    # Schwelle (Deployment 36 C wie der Notfallschutz), ist
                    # Durchkuehlen schlimmer als Schichtung - dann heizen.
                    # Befund 07.10.2026: frueher blockte der Zweig die Regel
                    # komplett; der Notfallschutz (Prio 110) griff erst unter
                    # seiner eigenen Schwelle -> Luecke ohne Heizentscheid.
                    notfall_grenze = (
                        float(notfall_einschalten_c)
                        if notfall_einschalten_c is not None
                        else abw.solltemperatur_c - max(
                            getattr(abw, "netz_notfall_offset_k", 8.0), 0.0
                        )
                    )
                    if temp <= notfall_grenze:
                        result.einschalten = True
                        result.grund = (
                            f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
                            f"+{abweichung:.1f}K >= +{abw.einschalten_bei_abweichung_k}K, "
                            f"oben {temp_oben:.1f}C >= {abw.schichtung_min_oben_c:.1f}C, "
                            f"aber {abw.temperaturfuehler} {temp:.1f}C <= {notfall_grenze:.1f}C "
                            f"(Notfall-Tiefgrenze) -> EIN trotz Schichtungsschutz"
                        )
                        return result
                    result.einschalten = None
                    result.grund = (
                        f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
                        f"+{abweichung:.1f}K >= +{abw.einschalten_bei_abweichung_k}K, "
                        f"oben {temp_oben:.1f}C >= {abw.schichtung_min_oben_c:.1f}C -> "
                        f"kein Start (Schichtungsschutz, schichtung_erlaube_start=false)"
                    )
                    return result
        # Quellen-Gate mit Tiefenschutz: Im Normalfall auf PV/Batterie warten
        # statt mit Netzstrom zu heizen. Erst wenn der Fuehler unter
        # (Soll - netz_notfall_offset_k) faellt, erlaubt der Tiefschutz Netz.
        #
        # Wetterlage-Ausnahme: "Auf PV warten" lohnt nur, wenn heute auch PV
        # kommt. Bei trueber Wetterlage wuerde die Regel sonst den ganzen Tag
        # auf eine Sonne warten, die ausbleibt - der Speicher laeuft kalt und
        # uebernimmt nur noch der Notfallschutz (Prio 110) mit seinem
        # 36/38-C-Band. Deshalb wird das Gate aufgehoben, sobald die
        # Tagesprognose unter `quelle_warten_min_forecast_wh_qm` liegt.
        warteschwelle = float(getattr(abw, "quelle_warten_min_forecast_wh_qm", 0.0) or 0.0)
        eff_fc = _forecast_effektiv_wh_qm(forecast_today_wh_qm, fc_ratio) \
            if forecast_today_wh_qm is not None else None
        unwetterlage = (
            warteschwelle > 0.0
            and eff_fc is not None
            and eff_fc < warteschwelle
            # Veraltete Solardaten sind kein Beleg fuer truebe Wetterlage -
            # im Zweifel gilt das fail-safe (weiter warten).
            and not solar_stale
        )
        if getattr(abw, "quelle_warten", True) and not unwetterlage:
            tief_grenze = abw.solltemperatur_c - max(
                getattr(abw, "netz_notfall_offset_k", 8.0), 0.0
            )
            if temp > tief_grenze and not _energiequelle_ok(
                feedin_watt,
                soc,
                getattr(abw, "pv_einspeisung_min_watt", 50.0),
                getattr(abw, "soc_min_prozent", 90.0),
                getattr(abw, "max_netzbezug_watt", -50.0),
                pv_acpower=pv_acpower,
                battery_power=battery_power,
                battery_min_watt=getattr(abw, "batterie_entladung_min_watt", 50.0),
                solar_stale=solar_stale,
                pv_erlaubt=pv_quelle_erlaubt,
            ):
                result.einschalten = None
                result.grund = (
                    f"Soll {abw.solltemperatur_c}C - oben "
                    f"{temp:.1f}C = +{abweichung:.1f}K >= "
                    f"+{abw.einschalten_bei_abweichung_k}K, aber keine Quelle "
                    f"(PV {feedin_watt:.0f}W) -> wartet auf PV/Batterie "
                    f"(Netz erst unter {tief_grenze:.1f}C)"
                )
                return result

        result.einschalten = True
        if unwetterlage:
            result.grund = (
                f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
                f"+{abweichung:.1f}K >= +{abw.einschalten_bei_abweichung_k}K -> EIN "
                f"(Prognose {eff_fc:.0f} Wh/m2 < {warteschwelle:.0f}: kein PV zu "
                f"erwarten, Netzbetrieb)"
            )
        else:
            result.grund = (
                f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
                f"+{abweichung:.1f}K >= +{abw.einschalten_bei_abweichung_k}K -> EIN"
            )
        return result

    # In der Hysterese: Keine Aktion (Kompressor laeuft weiter/bleibt aus)
    result.einschalten = None
    result.grund = (
        f"In Hysterese: Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
        f"{abweichung:.1f}K (zwischen {abw.ausschalten_bei_abweichung_k}K und {abw.einschalten_bei_abweichung_k}K)"
    )
    return result


# evaluate_forecast ist in regel_forecast.py ausgelagert.
from regel_forecast import evaluate_forecast  # noqa: E402, F401

# evaluate_adaptive_pv ist in regel_adaptive_pv.py ausgelagert.
from regel_adaptive_pv import evaluate_adaptive_pv  # noqa: E402, F401

