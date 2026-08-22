# -*- coding: utf-8 -*-
"""
Tests fuer api.build_mode_payload().

Regression: Das Webinterface zeigte den Sommer-Modus immer als 'Inaktiv',
weil api.py die Werte unter state.control suchte. Sie liegen aber am
State-Root bzw. in der Priority-Config.
"""
import os
import sys
from types import SimpleNamespace

import pytz

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from api import build_mode_payload


def baue_vollen_state():
    """State mit allen vom Builder gelesenen Attributen."""
    control = SimpleNamespace(
        previous_modus="Normalmodus",
        solar_ueberschuss_aktiv=False,
        active_rule_name=None,
        active_rule_sensor=None,
        blocking_reason=None,
        _soll_einschalten=False,
    )
    return SimpleNamespace(
        control=control,
        local_tz=pytz.timezone("Europe/Berlin"),
        urlaubsmodus_aktiv=False,
        bademodus_aktiv=False,
        sommer_modus_aktiv=True,
        sommer_modus_zaehler=3,
        priority_config=SimpleNamespace(
            sommer_modus=SimpleNamespace(
                temperatur_offset_c=-3.0,
                benoetigte_tage=3,
            )
        ),
    )


class TestSommerModusImModePayload:

    def test_werte_kommen_aus_den_richtigen_quellen(self):
        """KERN-TEST: aktiv/zaehler vom State-Root, offset/tage aus der Config."""
        payload = build_mode_payload(baue_vollen_state(), priority_info_override={})

        assert payload["sommer_modus_aktiv"] is True
        assert payload["sommer_modus_tage_ueber"] == 3
        assert payload["sommer_modus_benoetigte"] == 3
        assert payload["sommer_modus_offset_c"] == -3.0

    def test_inaktiver_modus_und_nullzaehler(self):
        state = baue_vollen_state()
        state.sommer_modus_aktiv = False
        state.sommer_modus_zaehler = 0

        payload = build_mode_payload(state, priority_info_override={})

        assert payload["sommer_modus_aktiv"] is False
        assert payload["sommer_modus_tage_ueber"] == 0

    def test_fehlende_attribute_liefern_sichere_defaults(self):
        """Alter/teilweiser State darf keinen Crash verursachen."""
        payload = build_mode_payload(SimpleNamespace(), priority_info_override=None)

        assert payload["sommer_modus_aktiv"] is False
        assert payload["sommer_modus_tage_ueber"] == 0
        assert payload["sommer_modus_benoetigte"] == 3
        assert payload["sommer_modus_offset_c"] == 0.0
        assert payload["current"] == ""
        assert payload["nightsperre_active"] is False

    def test_nightsperre_aus_priority_info(self):
        payload = build_mode_payload(
            baue_vollen_state(),
            priority_info_override={"nachtsperre_aktiv": True},
        )
        assert payload["nightsperre_active"] is True

    def test_webapp_erwartete_schluessel_vorhanden(self):
        """Der Vertrag mit webapp/index.html (sommer-info-Zeile)."""
        erwartet = {
            "sommer_modus_aktiv", "sommer_modus_offset_c",
            "sommer_modus_tage_ueber", "sommer_modus_benoetigte",
        }
        payload = build_mode_payload(baue_vollen_state())
        fehlen = erwartet - set(payload.keys())
        assert not fehlen, f"Webapp-benoetigte Schluessel fehlen: {fehlen}"
