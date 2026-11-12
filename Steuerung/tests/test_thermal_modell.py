# -*- coding: utf-8 -*-
"""Tests fuer das dreischichtige thermische Speichermodell (thermal_modell.py).

Das Modell wird von ``regel_helpers._etappen_plan`` (Etappenrechnung in
``evaluate_calculated_start``) per Vorwaertssimulation genutzt, war aber
bisher nur indirekt (ueber die Regel-Tests) abgedeckt. Diese Tests nageln
die Kernmechanik fest: Schichtung, Waermeeintrag, Verluste und Zapfung.
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from thermal_modell import (  # noqa: E402
    C_WASSER_WH_K,
    Speicher,
    SpeicherParameter,
    Schicht,
)


class TestSpeicherGrundlagen:
    def test_konstruktion_erzeugt_drei_schichten(self):
        s = Speicher()
        assert len(s.schichten) == 3
        assert [x.name for x in s.schichten] == ["unten", "mitte", "oben"]
        assert abs(sum(x.volumen_l for x in s.schichten) - s.p.volumen_l) < 1e-9

    def test_starttemperaturen_werden_gesetzt(self):
        s = Speicher(start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        assert s.unten.temperatur == 40.0
        assert s.mitte.temperatur == 42.0
        assert s.oben.temperatur == 44.0

    def test_temperaturen_dict(self):
        s = Speicher()
        t = s.temperaturen
        assert set(t.keys()) == {"unten", "mitte", "oben"}
        assert all(isinstance(v, float) for v in t.values())

    def test_setze_temperaturen(self):
        s = Speicher()
        s.setze_temperaturen(30.0, 35.0, 40.0)
        assert s.unten.temperatur == 30.0
        assert s.mitte.temperatur == 35.0
        assert s.oben.temperatur == 40.0

    def test_kapazitaet_wh_k_ist_volumen_mal_c(self):
        schicht = Schicht("x", 100.0, 50.0)
        assert schicht.kapazitaet_wh_k == 100.0 * C_WASSER_WH_K


class TestMitteltemperatur:
    def test_mitteltemperatur_gepunktet_nach_volumen(self):
        s = Speicher(start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        mittel = s.mitteltemperatur
        assert 41.5 < mittel < 42.5

    def test_mitteltemperatur_ist_gewichtetes_mittel(self):
        s = Speicher()
        s.setze_temperaturen(0.0, 0.0, 0.0)
        assert s.mitteltemperatur == 0.0


class TestGesamtenergie:
    def test_gesamtenergie_ueber_umgebung(self):
        s = Speicher(start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        assert s.gesamtenergie_wh > 0.0

    def test_gesamtenergie_kalt_ist_null(self):
        p = SpeicherParameter(umgebung_c=50.0)
        s = Speicher(parameter=p, start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        assert s.gesamtenergie_wh == 0.0


class TestSchritt:
    def test_schritt_ohne_kompressor_kuehlt_ab(self):
        s = Speicher(start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        alt = s.mitteltemperatur
        s.schritt(dt_s=600.0, kompressor_ein=False)
        assert s.mitteltemperatur < alt

    def test_schritt_mit_kompressor_heizt_auf(self):
        p = SpeicherParameter(
            heizleistung_w=1227.9, leistung_pro_k=0.0,
            konvektion_an_w_k=0.0, konvektion_oben_an_w_k=0.0,
            verlust_w_k=0.0, umgebung_c=20.0, waerme_verteilt=False,
            einspeisung_anteil_unten=0.42,
        )
        s = Speicher(parameter=p, start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        alt = s.mitteltemperatur
        s.schritt(dt_s=600.0, kompressor_ein=True)
        assert s.mitteltemperatur > alt

    def test_schritt_negativer_zeitschritt_ist_noop(self):
        s = Speicher(start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        alt = s.mitteltemperatur
        assert s.schritt(dt_s=0.0, kompressor_ein=True) == 0.0
        assert s.schritt(dt_s=-10.0, kompressor_ein=True) == 0.0
        assert s.mitteltemperatur == alt

    def test_schritt_kompressor_aus_liefert_keine_waerme(self):
        p = SpeicherParameter(verlust_w_k=0.0, umgebung_c=20.0)
        s = Speicher(parameter=p, start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        q = s.schritt(dt_s=600.0, kompressor_ein=False)
        assert q == 0.0

    def test_schritt_kompressor_an_liefert_positive_waerme(self):
        p = SpeicherParameter(
            heizleistung_w=1227.9, leistung_pro_k=0.0,
            konvektion_an_w_k=0.0, konvektion_oben_an_w_k=0.0,
            verlust_w_k=0.0, umgebung_c=20.0, waerme_verteilt=False,
        )
        s = Speicher(parameter=p, start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        q = s.schritt(dt_s=600.0, kompressor_ein=True)
        assert q > 0.0
        assert abs(q - 1227.9 * (600.0 / 3600.0)) < 1.0

    def test_temperaturen_bleiben_in_grenzen(self):
        p = SpeicherParameter(
            heizleistung_w=100000.0, leistung_pro_k=0.0,
            konvektion_an_w_k=0.0, konvektion_oben_an_w_k=0.0,
            verlust_w_k=0.0, umgebung_c=20.0, waerme_verteilt=False,
        )
        s = Speicher(parameter=p, start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        for _ in range(100):
            s.schritt(dt_s=3600.0, kompressor_ein=True)
        for schicht in s.schichten:
            assert 1.0 <= schicht.temperatur <= 99.0


class TestZapfe:
    def test_zapfe_negativ_ist_noop(self):
        s = Speicher(start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        alt = s.mitteltemperatur
        assert s.zapfe(volumen_l=-10.0) == 0.0
        assert s.zapfe(volumen_l=0.0) == 0.0
        assert s.mitteltemperatur == alt

    def test_zapfe_kuehlt_unten_am_staerksten(self):
        s = Speicher(start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        unten_vor = s.unten.temperatur
        oben_vor = s.oben.temperatur
        s.zapfe(volumen_l=50.0)
        assert s.unten.temperatur < unten_vor
        assert s.oben.temperatur < oben_vor
        assert (unten_vor - s.unten.temperatur) > (oben_vor - s.oben.temperatur)

    def test_zapfe_liefert_entnommene_waerme_positiv(self):
        # Der Speicher ist wärmer als das Kaltwasser -> die entnommene
        # Wärme (Wärmeverlust des Speichers) ist als positiver Betrag
        # zurückgegeben (return = -(Summe der negativen dE)).
        s = Speicher(start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        q = s.zapfe(volumen_l=50.0)
        assert q > 0.0

    def test_zapfe_mit_kaelterem_kaltwasser_liefert_mehr_entnahme(self):
        s1 = Speicher(start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        s2 = Speicher(start_unten=40.0, start_mitte=42.0, start_oben=44.0)
        q1 = s1.zapfe(volumen_l=50.0, temperatur_c=20.0)
        q2 = s2.zapfe(volumen_l=50.0, temperatur_c=5.0)
        assert abs(q2) > abs(q1)


class TestParameter:
    def test_kopie_ist_selbststaendig(self):
        p = SpeicherParameter(heizleistung_w=1227.9)
        k = p.kopie()
        k.heizleistung_w = 999.0
        assert p.heizleistung_w == 1227.9
        assert k.heizleistung_w == 999.0

    def test_gemessen_oktober_2026_liefert_kalibrierte_werte(self):
        p = SpeicherParameter.gemessen_oktober_2026()
        assert p.volumen_l == 300.0
        assert p.heizleistung_w > 0.0
        assert p.leistung_pro_k > 0.0
        assert p.heizleistung_w != SpeicherParameter().heizleistung_w