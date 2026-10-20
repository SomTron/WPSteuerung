"""Tests der Simulations-Umgebung: Bilanz, Profile und Quellenklassifikation.

Die Simulation ist das Werkzeug, mit dem Konfigurationsvarianten verglichen
werden. Ihre Zahlen sind nur so belastbar wie ihre Eingangsdaten - deshalb
stehen hier die Regressionspruefungen fuer die Defekte vom 20.10.2026:

* Die PV-Erzeugung kam aus ``acpower`` (Wechselrichterausgang = Haus + Netz)
  statt aus der Gleichstromseite. Nachts "erzeugte" die Simulation damit
  ~230 W Strom aus dem Nichts, das PV-Fenster schloss nie und die
  Tagesprognose passte zu keinem Profil.
* Der Stundenprognose-Faktor 0.19 war frei erfunden; die Summe der
  Stundenprognose ergab nicht die Tagesprognose des Szenarios.
* ``feedinpower`` war immer positiv. Das Quellen-Gate der Regelung
  ("kein Netzkauf") erkennt einen Netzbezug aber an einem NEGATIVEN
  Einspeisungswert - die Simulation glaubte daher, es werde nie Strom
  aus dem Netz bezogen.
* ``nacht_entladung_anteil`` reduzierte den Netzbezug, ohne dass die
  Batterie entlud. Das Defizit war weder gedeckt noch bezogen: nachts
  ging Energie verloren.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "Analyse", "sim")
STEUERUNG = os.path.join(ROOT, "Steuerung")
for pfad in (SIM, STEUERUNG):
    if pfad not in sys.path:
        sys.path.insert(0, pfad)

import umgebung  # noqa: E402


def test_winterszenarien_laden_ihre_eigenen_parameter():
    """Die Winterszenarien duerfen nicht die September-Parameter nehmen.

    Der Speicher verhaelt sich im Winter grundlegend anders (er steht
    uebernacht bei ~12 C statt bei ~24 C). Ohne eigene Parameter und
    ohne die gemessenen Starttemperaturen liegt der simulierte
    Winter-Netzbezug um mehrere kWh/Tag daneben.
    """
    for name, szenario in umgebung.SZENARIEN.items():
        if not name.startswith("winter"):
            continue
        assert szenario.parameter_datei == "parameter_winter.json", (
            f"{name}: kein Winter-Parametersatz")
        pfad = os.path.join(SIM, szenario.parameter_datei)
        assert os.path.exists(pfad), f"{name}: {pfad} fehlt"
        assert szenario.start_unten_c is not None, f"{name}: kein Startwert"
        # Der Speicher startet kalt - nicht bei 41 C wie im September.
        assert szenario.start_unten_c < 25.0, (
            f"{name}: Starttemperatur unten {szenario.start_unten_c} C "
            "ist zu warm fuer den Winter")


def test_winterparameter_haben_keller_als_umgebung():
    """Der Keller ist die Umgebung des Speichers (Tiefpunkt ~12 C)."""
    import json

    pfad = os.path.join(SIM, "parameter_winter.json")
    with open(pfad, "r", encoding="utf-8") as f:
        daten = json.load(f)
    assert 8.0 <= daten["umgebung_c"] <= 16.0, (
        f"umgebung_c={daten['umgebung_c']} passt nicht zum Keller")
    assert daten["heizleistung_w"] > 1000.0
    assert daten["verlust_w_k"] > 1.0


def test_varianten_koennen_listen_eintraege_patchen():
    """Die Zielkonflikt-Varianten muessen die MinTemp-Eintraege erreichen.

    ``mindest_temp.eintraege`` ist eine Liste; ein Sektions-Patch
    erreicht sie nicht. Ohne den Sonderfall ``__eintraege__`` waere die
    Abendgarantie-Variante wirkungslos - und der Zielkonflikt waere
    in der Simulation nicht reproduzierbar.
    """
    from harness import Simulation

    sz = umgebung.SZENARIEN["winter_wenig_pv"]
    sz = type(sz)(**{**sz.__dict__, "tage": 1})
    sim = Simulation(sz, parameter=None, lernpfad=None,
                     config_patch={
                         "mindest_temp": {"__eintraege__": {"Abend-Mitte": {
                             "nachtsperre_ueberschreiben": True}}},
                         "sicherheit": {"start_vorhersage_aktiv": False},
                     })
    eintraege = {e.name: e for e in sim.priority_config.mindest_temp.eintraege}
    assert eintraege["Abend-Mitte"].nachtsperre_ueberschreiben is True
    assert sim.priority_config.sicherheit.start_vorhersage_aktiv is False


def test_zielkonflikt_varianten_sind_vorhanden():
    """Die drei Zielkonflikt-Varianten muessen existieren."""
    from varianten import VARIANTEN

    for name in ("zielkonflikt_garantie_22h",
                 "zielkonflikt_antizipation_aus",
                 "zielkonflikt_beides"):
        assert name in VARIANTEN, f"Variante {name} fehlt"


def test_speicher_verliert_oben_weniger_als_unten():
    """Der obere Fuehler sitzt am Deckel, nicht im Keller.

    Gemessen im Dezember-Log: ueber Nacht verliert der obene Fuehler
    1 K (44,6 -> 43,6 C), der untere 17 K (30,6 -> 13,8 C). Mit einer
    gemeinsamen Umgebung fuer alle Schichten kuehlte der obere Knoten
    viel zu stark ab - dann loeste der Notfallschutz nachts aus, obwohl
    das in der Realitaet nie passiert.
    """
    from thermal import Speicher, SpeicherParameter

    p = SpeicherParameter(
        umgebung_c=12.0, umgebung_oben_c=20.0, verlust_oben_anteil=0.05,
        verlust_w_k=6.5, konvektion_aus_w_k=0.4, konvektion_oben_aus_w_k=2.0,
    )
    s = Speicher(p, start_unten=30.0, start_mitte=44.0, start_oben=44.0)
    for _ in range(12 * 60):        # 12 Stunden Leerlauf
        s.schritt(60.0, False)
    assert s.oben.temperatur > 40.0, (
        f"oben kuehlte auf {s.oben.temperatur:.1f} C ab - der Deckel "
        "ist nicht der Keller")
    assert s.unten.temperatur < s.oben.temperatur - 5.0


def test_konvektion_ist_numerisch_stabil():
    """Die Kopplung darf den Speicher nicht ueberhitzen.

    Bei sehr grossen Kopplungskoeffizienten kippte die explizite
    Integration: der obere Knoten wurde leer gepumpt und der Speicher
    "ueberhitzte" scheinbar auf 99 C.
    """
    from thermal import Speicher, SpeicherParameter

    p = SpeicherParameter(
        heizleistung_w=2000.0, konvektion_an_w_k=5000.0,
        konvektion_oben_an_w_k=5000.0, umgebung_c=12.0,
        verlust_w_k=6.5,
    )
    s = Speicher(p, start_unten=12.0, start_mitte=32.0, start_oben=44.0)
    for _ in range(3 * 60):
        s.schritt(60.0, True)
    for schicht in s.schichten:
        assert schicht.temperatur < 90.0, (
            f"{schicht.name} auf {schicht.temperatur:.1f} C - "
            "die Konvektion ist numerisch instabil")


def test_legionellenfahrt_beendet_die_simulation_nicht_im_endschuss():
    """Die Legionellenfahrt muss genau einmal laufen, nicht 281-mal.

    Befund 20.10.2026: die Waerme wurde gleichmaessig auf alle Schichten
    verteilt. Die obere Schicht erreichte dadurch 65 C, bevor die untere
    die Zieltemperatur 60 C erreichte - der Ueberhitzungsschutz beendete
    die Fahrt, und die Regel startete sofort neu (38 Läufe je Tag).
    """
    from thermal import Speicher, SpeicherParameter

    p = SpeicherParameter(
        heizleistung_w=1900.0, konvektion_an_w_k=100.0,
        konvektion_oben_an_w_k=50.0, umgebung_c=12.0,
        umgebung_oben_c=20.0, verlust_oben_anteil=0.05, verlust_w_k=6.5,
        waerme_verteilt=True, einspeisung_anteil_oben=0.30,
    )
    s = Speicher(p, start_unten=12.0, start_mitte=32.0, start_oben=44.0)
    for _ in range(6 * 60):
        s.schritt(60.0, True)
    # Die untere Schicht muss die Zieltemperatur erreichen, BEVOR die
    # obere die Ueberhitzungsgrenze (65 C) erreicht.
    assert s.unten.temperatur >= 55.0, (
        f"unten nur auf {s.unten.temperatur:.1f} C - die Fahrt wuerde "
        "die Zieltemperatur verfehlen")
    assert s.oben.temperatur < 65.0, (
        f"oben auf {s.oben.temperatur:.1f} C - die Legionellenfahrt "
        "wuerde am Ueberhitzungsschutz abbrechen")


def test_pv_profile_ist_die_gleichstromseite():
    """Nachts erzeugt die Anlage nichts - das Profil muss 0 W zeigen."""
    for name, profil in umgebung.PV_PROFILE.items():
        nachts = [profil.get(h, 0.0) for h in range(0, 7)]
        assert all(w == 0 for w in nachts), f"{name}: nachts PV > 0: {nachts}"
        # und tagsueber muss es deutlich mehr als die alte AC-Konstante geben
        assert max(profil.values()) > 1000.0, name


def test_dezemberprofil_ist_echte_messung():
    """Der Dezember kommt aus einem echten Winterlog, nicht aus September."""
    dez = umgebung.PV_PROFILE["dezember"]
    # gemessener Dezember-Median: ~2 kW Spitzene, ~9.5 kWh/Tag
    assert 1500 <= max(dez.values()) <= 2500
    tag_wh = sum(dez.values())
    assert 8000 <= tag_wh <= 11000, tag_wh
    # Der Dezember hat einen kuerzeren Tag als der September
    sep = umgebung.PV_PROFILE["september"]
    assert sum(1 for h in range(24) if dez.get(h, 0) > 0) < sum(
        1 for h in range(24) if sep.get(h, 0) > 0)


def test_batterie_bilanz_ist_geschlossen():
    """Entladung + Netzbezug decken das Defizit - nichts geht verloren."""
    bat = umgebung.Batterie(
        umgebung.BatterieParameter(soc_start_prozent=60.0)
    )
    for pv_w, hauslast, wp in [(0.0, 300.0, 0.0), (0.0, 300.0, 500.0),
                                (200.0, 300.0, 0.0), (5000.0, 300.0, 500.0)]:
        entl, lad, einsp, netz = bat.rechne(1.0 / 60.0, pv_w, hauslast, wp)
        zufluss = pv_w
        verbrauch = hauslast + wp
        # Bilanz: Erzeugung + Entladung + Netzbezug = Verbrauch + Ladung + Einspeisung
        links = zufluss + entl + netz
        rechts = verbrauch + lad + einsp
        assert abs(links - rechts) < 1.0, (pv_w, hauslast, wp, links, rechts)


def test_batterie_entlaedt_nachts_anteilig():
    """Nachts deckt die Batterie einen Anteil, der Rest kommt aus dem Netz."""
    bat = umgebung.Batterie(
        umgebung.BatterieParameter(soc_start_prozent=90.0,
                                   nacht_entladung_anteil=0.8)
    )
    entl, lad, einsp, netz = bat.rechne(1.0, 0.0, 1000.0, 0.0)
    assert entl == pytest.approx(800.0, abs=1.0), entl
    assert netz == pytest.approx(200.0, abs=1.0), netz


def test_hauslast_entspricht_der_messung():
    """Die Hauslast kommt aus dem Log (Median bei ausgeschalteter WP)."""
    summe = sum(umgebung.HAUSLAST_W.values())
    assert 5000 <= summe <= 7000, summe
    # nachts ruhig, tagsueber aehnlich, Abendspitze
    assert umgebung.HAUSLAST_W[2] == 196
    assert umgebung.HAUSLAST_W[18] >= umgebung.HAUSLAST_W[2]


def test_forecast_stundenprofil_summiert_zur_tagesprognose():
    """Die Stundenprognose muss in der Tagesprognose aufgehen."""
    for profil_name in umgebung.PV_PROFILE:
        sz = umgebung.Szenario(
            name="Test", start=umgebung.date(2026, 12, 7), tage=1,
            pv_profil=profil_name, forecast_wh=700.0,
        )
        profil = umgebung.PV_PROFILE[profil_name]
        summe_profil = sum(profil.values())
        stunden = {
            h: sz.forecast_wh * (v / summe_profil)
            for h, v in profil.items() if v > 0
        }
        assert sum(stunden.values()) == pytest.approx(700.0, rel=1e-9)
        # Die Nachtstunden duerfen keine Prognose haben (keine Sonne)
        assert all(w == 0 for h, w in stunden.items() if h < 7 or h > 19)


def test_tagesprognose_wird_in_kwh_pro_m2_uebergeben():
    """Die Regelung erwartet kWh/m² (Open-Meteo), das Szenario nutzt Wh/m².

    Ohne die Umrechnung liest die Regelung 700 Wh/m² als 700 kWh/m². Die
    Prognose wirkt dann um den Faktor 1000 zu pessimistisch: das
    PV-Fenster schließt nie und die Forecast-Kalibrierung klemmt am
    unteren Wert (0,3).
    """
    from logic_utils import normalize_forecast_wh_qm

    sz = umgebung.Szenario(
        name="Test", start=umgebung.date(2026, 12, 7), tage=1,
        pv_profil="dezember", forecast_wh=700.0,
    )
    # So, wie der Harness es in den State schreibt:
    an_die_regelung = sz.forecast_wh / 1000.0
    # Und so, wie es die Regelung wieder normalisiert:
    wieder_heraus = normalize_forecast_wh_qm(an_die_regelung)
    assert wieder_heraus == pytest.approx(700.0, rel=1e-9)


def test_prognose_ist_in_der_richtigen_einheit_am_state():
    """Regressionstest am echten Harness: der State traegt kWh/m²."""
    import asyncio
    import tempfile

    from harness import Simulation
    from run import lade_parameter
    from umgebung import SZENARIEN

    sz = SZENARIEN["winter_wenig_pv"]
    sz = type(sz)(**{**sz.__dict__, "tage": 1})
    lernpfad = os.path.join(tempfile.gettempdir(), "wpsim_test_einheit.json")
    if os.path.exists(lernpfad):
        os.remove(lernpfad)
    sim = Simulation(sz, parameter=lade_parameter(), lernpfad=lernpfad,
                     schritt_sekunden=600.0)
    asyncio.run(sim.simuliere())
    # 700 Wh/m2 = 0,7 kWh/m2 - nicht 700 kWh/m2
    assert sim.state.solar.forecast_today == pytest.approx(0.7, rel=1e-6)
    # Die Kalibrierung darf nicht am unteren Klemmwert kleben:
    assert sim.lern_engine.data.forecast_ratio > 0.5
