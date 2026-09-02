"""
Tests fuer die neuen Anforderungen (Task: Verbesserungen).

Abgedeckt:
1. Debounce-Schaltverzug: soll_einschalten wird erst an die Hardware
   uebergeben, wenn der Gewinner-Moduswechsel per Debounce bestaetigt ist.
2. PV-Mindestlaufzeit entkoppeln: Nach Ablauf der Hardware-Schutzzeit
   (zyklus.pv_min_laufzeit_minuten, 10-15 min) darf ein PV-Einbruch die WP
   abschalten, ohne 60 Minuten Netzbezug zu erzwingen.
3. Legionellen vs. Ueberhitzungsschutz: Waehrend aktiver Legionellenfahrt
   wird ueberhitzung_c dynamisch auf legionellen_max_temp_c angehoben.
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytz
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import priority_control_logic as pcl  # noqa: E402
from json_config import WPSteuerungConfig  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


def _min_state(previous="Keine Regel aktiv"):
    """Kompakter, echter State fuer determine_mode_and_setpoints."""
    cfg = WPSteuerungConfig()
    return SimpleNamespace(
        local_tz=TZ,
        priority_config=cfg,
        bademodus_aktiv=False,
        urlaubsmodus_aktiv=False,
        sommer_modus_aktiv=False,
        legionellen_aktiv=False,
        legionellen_last_done=None,
        legionellen_started_at=None,
        sensors=SimpleNamespace(t_oben=45.0, t_unten=42.0,
                                t_mittig=43.0, t_verd=30.0),
        solar=SimpleNamespace(
            feedinpower=0.0, batpower=0.0, soc=80.0,
            forecast_today=None, forecast_tomorrow=None, forecast_day2=None,
            last_api_call=TZ.localize(datetime(2026, 1, 15, 11, 50)),
        ),
        control=SimpleNamespace(
            kompressor_ein=False,
            previous_modus=previous,
            aktueller_einschaltpunkt=None,
            aktueller_ausschaltpunkt=None,
            active_rule_name=None,
            active_rule_sensor=None,
            komfort_aktiv=False,
            alle_ergebnisse=[],
            _soll_einschalten=False,
            _soll_einschalten_bestaetigt=False,
            _lauf_start_regel=None,
            _pending_modus=None,
            _wechsel_historie=None,
            restart_lockout_until=None,
            boiler_max_blockiert=None,
            blocking_reason=None,
        ),
        stats=SimpleNamespace(
            last_compressor_on_time=TZ.localize(datetime(2026, 1, 15, 11, 55)),
            last_compressor_off_time=TZ.localize(datetime(2026, 1, 15, 11, 50)),
        ),
    )


# ============================================================
# 1) Debounce-Schaltverzug
# ============================================================
class TestDebounceSchaltverzug:
    @pytest.mark.asyncio
    async def test_erste_bewertung_noch_nicht_an_hardware(self):
        """Gewinner-Wechsel 'Abweichung' pendelt gerade: Nach dem 1. Loop wird
        soll_einschalten NICHT an die Hardware uebergeben (bleibt False)."""
        import priority_control as pc
        from unittest.mock import patch

        state = _min_state()
        gewinner = pc.RegelErgebnis(
            name="Abweichung", prioritaet=47, aktiv=True, einschalten=True,
            grund="Soll 44.0C - unten 40.0C = +4.0K -> EIN",
        )
        with patch("priority_control_logic.bewerte_alle_regeln",
                   return_value=(gewinner, [gewinner])):
            res = await pcl.determine_mode_and_setpoints(state, 40.0, 43.0)

        # Debounce noch nicht bestaetigt -> kein Schaltsignal an HW
        assert res["soll_einschalten"] is False
        assert state.control.previous_modus == "Keine Regel aktiv"

    @pytest.mark.asyncio
    async def test_zweite_bestaetigte_bewertung_schaltet(self):
        """Erst nach der 2. identischen Bewertung (Debounce bestaetigt) wird
        soll_einschalten=True an die Hardware uebergeben und previous_modus
        aktualisiert."""
        import priority_control as pc
        from unittest.mock import patch

        state = _min_state()
        gewinner = pc.RegelErgebnis(
            name="Abweichung", prioritaet=47, aktiv=True, einschalten=True,
            grund="Soll 44.0C - unten 40.0C = +4.0K -> EIN",
        )
        for _loop in (1, 2):
            with patch("priority_control_logic.bewerte_alle_regeln",
                       return_value=(gewinner, [gewinner])):
                res = await pcl.determine_mode_and_setpoints(state, 40.0, 43.0)

        assert res["soll_einschalten"] is True
        assert state.control.previous_modus == "Abweichung"

    @pytest.mark.asyncio
    async def test_ohne_wechsel_sofort_durchgereicht(self):
        """Ohne Wechsel (gleicher Gewinner wie vorher) passiert kein Debounce -
        soll_einschalten wird sofort durchgereicht."""
        import priority_control as pc
        from unittest.mock import patch

        state = _min_state(previous="Abweichung")
        gewinner = pc.RegelErgebnis(
            name="Abweichung", prioritaet=47, aktiv=True, einschalten=True,
            grund="Soll 44.0C - unten 40.0C = +4.0K -> EIN",
        )
        with patch("priority_control_logic.bewerte_alle_regeln",
                   return_value=(gewinner, [gewinner])):
            res = await pcl.determine_mode_and_setpoints(state, 40.0, 43.0)

        assert res["soll_einschalten"] is True
        assert state.control.previous_modus == "Abweichung"

# ============================================================
# 2) PV-Mindestlaufzeit entkoppeln
# ============================================================
class TestPvMindestlaufzeitEntkoppelt:
    def _state(self, lauf_start_regel="PV_unten", laufzeit_minuten=12):
        state = _min_state()
        state.control.kompressor_ein = True
        state.control._soll_einschalten = False
        state.control._lauf_start_regel = lauf_start_regel
        state.stats.last_compressor_on_time = (
            datetime.now(state.local_tz) - timedelta(minutes=laufzeit_minuten)
        )
        return state

    @pytest.mark.asyncio
    async def test_pv_lauf_darf_nach_schutzzeit_abschalten(self):
        """PV-Lauf (PV_unten) + 12 min (> Hardware-Schutzzeit 10 min): Ein
        PV-Einbruch darf abschalten, obwohl die volle Mindestlaufzeit 60 min
        waere."""
        state = self._state()
        calls = []

        async def set_status(state, ein, **kwargs):
            calls.append(ein)
            return True

        erg = await pcl.handle_compressor_off(
            state, None, regelfuehler=42.0, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=60), t_oben=45.0,
            set_kompressor_status_func=set_status, regel_name=None,
        )
        assert erg is True
        assert calls == [False]

    @pytest.mark.asyncio
    async def test_netz_lauf_braucht_volle_mindestlaufzeit(self):
        """'Abweichung' (Netzstrom-Lauf) braucht weiterhin die volle
        Mindestlaufzeit (60 min) - keine Entkopplung."""
        state = self._state(lauf_start_regel="Abweichung")

        async def set_status(state, ein, **kwargs):
            return True

        erg = await pcl.handle_compressor_off(
            state, None, regelfuehler=42.0, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=60), t_oben=45.0,
            set_kompressor_status_func=set_status, regel_name=None,
        )
        assert erg is False  # Mindestlaufzeit noch nicht erreicht
        assert "Mindestlaufzeit" in (state.control.blocking_reason or "")


# ============================================================
# 3) Legionellen vs. Ueberhitzungsschutz
# ============================================================
class TestLegionellenUeberhitzungBypass:
    def _state(self, legionellen_aktiv):
        return SimpleNamespace(
            priority_config=SimpleNamespace(
                sicherheit=SimpleNamespace(ueberhitzung_c=58.0),
                legionellen=SimpleNamespace(legionellen_max_temp_c=65.0),
            ),
            legionellen_aktiv=legionellen_aktiv,
        )

    def test_ohne_legionellen_58(self):
        assert pcl._effektive_ueberhitzung_schwelle(
            self._state(legionellen_aktiv=False)
        ) == 58.0

    def test_mit_legionellen_auf_65(self):
        assert pcl._effektive_ueberhitzung_schwelle(
            self._state(legionellen_aktiv=True)
        ) == 65.0

    def test_legionellen_max_kleiner_wird_ignoriert(self):
        state = self._state(legionellen_aktiv=True)
        state.priority_config.legionellen.legionellen_max_temp_c = 50.0
        assert pcl._effektive_ueberhitzung_schwelle(state) == 58.0


class TestLegionellenUeberhitzungCheckSafety:
    def _safety_state(self, kompressor_ein):
        return SimpleNamespace(
            local_tz=TZ,
            priority_config=SimpleNamespace(
                sicherheit=SimpleNamespace(
                    ueberhitzung_c=58.0, max_temp_c=48.0,
                ),
                legionellen=SimpleNamespace(legionellen_max_temp_c=65.0),
            ),
            legionellen_aktiv=True,
            control=SimpleNamespace(blocking_reason=None,
                                    kompressor_ein=kompressor_ein),
        )

    @pytest.mark.asyncio
    async def test_ueberhitzung_unter_bypass_kein_abschalten(self):
        """62C waehrend Legionellen (Bypass 65): kein Abschalten - Schutz ist
        dynamisch angehoben."""
        from unittest.mock import AsyncMock

        state = self._safety_state(kompressor_ein=False)
        setter = AsyncMock(return_value=None)
        erg = await pcl.check_safety_limits(
            None, state, t_oben=62.0, t_unten=40.0, t_mittig=50.0, t_verd=5.0,
            set_kompressor_status_func=setter,
        )
        assert erg is True
        setter.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ueber_legionellen_max_wird_abgeschaltet(self):
        from unittest.mock import AsyncMock

        state = self._safety_state(kompressor_ein=True)
        setter = AsyncMock(return_value=None)
        await pcl.check_safety_limits(
            None, state, t_oben=66.0, t_unten=40.0, t_mittig=50.0, t_verd=5.0,
            set_kompressor_status_func=setter,
        )
        assert "UEBERHITZUNG" in state.control.blocking_reason
        setter.assert_awaited_once_with(state, False, force=True)
