"""Tests fuer ConfigManager: config.ini -> Pydantic-Validierung -> Defaults-Fallback.

Der ConfigManager ist die Schaltzentrale zwischen Nutzer-Konfiguration und
Steuerungslogik. Kritische Eigenschaften:
1. Fehlende/defekte Datei darf den Start NIEMALS abwuerfen (Defaults greifen)
2. Typfehler in einzelnen Werten werden abgefangen (Pydantic ValidationError)
3. Gross-/Kleinschreibung der Schluessel bleibt erhalten (optionxform = str)
4. UTF-8-BOM aelterer Windows-Editoren bricht die erste Sektion nicht mehr
"""

from config_manager import ConfigManager


def schreibe_ini(pfad, inhalt, encoding="utf-8"):
    with open(pfad, "w", encoding=encoding) as f:
        f.write(inhalt)
    return str(pfad)


# ── Defaults ──────────────────────────────────────────────────────────

class TestDefaults:
    def test_fehlende_datei_liefert_defaults(self, tmp_path):
        cm = ConfigManager(config_path=str(tmp_path / "gibts_nicht.ini"))
        c = cm.get()
        assert c.Heizungssteuerung.MIN_LAUFZEIT == 15
        assert c.Heizungssteuerung.API_PORT == 8000
        assert c.Telegram.BOT_TOKEN == ""
        assert c.Solarueberschuss.BATPOWER_THRESHOLD == 600.0

    def test_leere_datei_ist_kein_fehler(self, tmp_path):
        pfad = schreibe_ini(tmp_path / "config.ini", "")
        cm = ConfigManager(config_path=pfad)
        assert cm.get().Heizungssteuerung.MIN_LAUFZEIT == 15


# ── Parsing ───────────────────────────────────────────────────────────

class TestParsing:
    def test_gueltige_ini_wird_geladen_mit_typkonvertierung(self, tmp_path):
        pfad = schreibe_ini(tmp_path / "config.ini",
                            "[Heizungssteuerung]\n"
                            "MIN_LAUFZEIT = 25\n"
                            "SICHERHEITS_TEMP = 55.5\n"
                            "[Telegram]\n"
                            "BOT_TOKEN = 123456:ABC-DEF\n"
                            "[Solarueberschuss]\n"
                            "BATPOWER_THRESHOLD = 750.5\n")
        c = ConfigManager(config_path=pfad).get()
        # Pydantic konvertiert INI-Strings in die Feldtypen
        assert c.Heizungssteuerung.MIN_LAUFZEIT == 25          # int
        assert c.Heizungssteuerung.SICHERHEITS_TEMP == 55.5    # float
        assert c.Solarueberschuss.BATPOWER_THRESHOLD == 750.5  # float
        assert c.Telegram.BOT_TOKEN == "123456:ABC-DEF"        # str unveraendert
        # Nicht gesetzte Felder bleiben auf Default
        assert c.Heizungssteuerung.API_PORT == 8000

    def test_gross_kleinschreibung_der_schluessel_wird_bewahrt(self, tmp_path):
        """optionxform=str: 'min_laufzeit' matcht NICHT das Feld MIN_LAUFZEIT."""
        pfad = schreibe_ini(tmp_path / "config.ini",
                            "[Heizungssteuerung]\nmin_laufzeit = 99\n")
        cm = ConfigManager(config_path=pfad)
        assert cm.get().Heizungssteuerung.MIN_LAUFZEIT == 15   # Default bleibt aktiv

    def test_unbekannte_sektionen_und_schluessel_werden_ignoriert(self, tmp_path):
        pfad = schreibe_ini(tmp_path / "config.ini",
                            "[Zukunftsexperiment]\n"
                            "EXPERIMENTELL = ja\n"
                            "[Heizungssteuerung]\n"
                            "NEUER_SCHLUESSEL_OHNE_FELD = 1\n")
        cm = ConfigManager(config_path=pfad)
        assert cm.get().Heizungssteuerung.MIN_LAUFZEIT == 15

    def test_get_liefert_dasselbe_objekt(self, tmp_path):
        cm = ConfigManager(config_path=str(tmp_path / "fehlt.ini"))
        assert cm.get() is cm.config


# ── Validierung ───────────────────────────────────────────────────────

class TestValidierung:
    def test_typfehler_behaelt_defaults(self, tmp_path, caplog):
        """API_PORT='keine_zahl' -> ValidationError wird gefangen, Defaults bleiben."""
        import logging
        pfad = schreibe_ini(tmp_path / "config.ini",
                            "[Heizungssteuerung]\nAPI_PORT = keine_zahl\n")
        with caplog.at_level(logging.ERROR):
            cm = ConfigManager(config_path=pfad)
        c = cm.get()
        assert c.Heizungssteuerung.API_PORT == 8000            # Default statt Crash
        assert any("Ung\u00fcltiger Wert" in r.message or "Ungueltiger Wert" in r.message for r in caplog.records)

    def test_fehler_in_einer_sektion_blockt_andere_sektionen_nicht(self, tmp_path):
        """Partielles Loading: Nur das fehlerhafte Feld faellt auf Default.

        Vorher: EIN Tippfehler warf die KOMPLETTE Config weg (auch Telegram).
        Jetzt: Gueltige Sektionen/Werte bleiben vollstaendig erhalten.
        """
        pfad = schreibe_ini(tmp_path / "config.ini",
                            "[Telegram]\nBOT_TOKEN = gueltig\n"
                            "[Heizungssteuerung]\nMIN_LAUFZEIT = nicht_zahl\n")
        c = ConfigManager(config_path=pfad).get()
        assert c.Telegram.BOT_TOKEN == "gueltig"                # andere Sektion laedt
        assert c.Heizungssteuerung.MIN_LAUFZEIT == 15           # nur dieses Feld -> Default

    def test_feldweiser_fallback_innerhalb_einer_sektion(self, tmp_path):
        """Ein kaputter Wert kapt nur sich selbst, Geschwisterwerte laden weiter."""
        pfad = schreibe_ini(tmp_path / "config.ini",
                            "[Heizungssteuerung]\n"
                            "MIN_LAUFZEIT = keine_zahl\n"
                            "SICHERHEITS_TEMP = 58.5\n"
                            "MIN_PAUSE = 35\n")
        c = ConfigManager(config_path=pfad).get().Heizungssteuerung
        assert c.MIN_LAUFZEIT == 15                             # Default (fehlerhaft)
        assert c.SICHERHEITS_TEMP == 58.5                       # geladen
        assert c.MIN_PAUSE == 35                                # geladen

    def test_unbekannte_schluessel_werden_gewarnt(self, tmp_path, caplog):
        """Tippfehler wie MIN_LAUFZET sind sonst still wirkungslos - jetzt Warnung."""
        import logging
        pfad = schreibe_ini(tmp_path / "config.ini",
                            "[Heizungssteuerung]\n"
                            "MIN_LAUFZET = 99\n"          # Tippfehler bewusst
                            "MIN_LAUFZEIT = 22\n")
        with caplog.at_level(logging.WARNING):
            cm = ConfigManager(config_path=pfad)
        assert cm.get().Heizungssteuerung.MIN_LAUFZEIT == 22    # korrekter Key wirkt
        assert any("MIN_LAUFZET" in r.message and "Tippfehler" in r.message
                   for r in caplog.records)


# ── Robustheit ────────────────────────────────────────────────────────

class TestRobustheit:
    def test_utf8_bom_wird_toleriert(self, tmp_path):
        """Aeltere Windows-Editoren speichern UTF-8 mit BOM.

        Ohne utf-8-sig wuerde die erste Sektion '\ufeffHeizungssteuerung'
        heissen und STILL ignoriert werden - der Klassiker fuer
        'meine Aenderungen wirken nicht'.
        """
        roh = b"\xef\xbb\xbf[Heizungssteuerung]\r\nMIN_LAUFZEIT = 33\r\n"
        pfad = str(tmp_path / "config.ini")
        with open(pfad, "wb") as f:
            f.write(roh)
        cm = ConfigManager(config_path=pfad)
        assert cm.get().Heizungssteuerung.MIN_LAUFZEIT == 33

    def test_crlf_zeilenenden_werden_toleriert(self, tmp_path):
        pfad = schreibe_ini(tmp_path / "config.ini",
                            "[Heizungssteuerung]\r\nMIN_LAUFZEIT = 21\r\n")
        assert ConfigManager(config_path=pfad).get().Heizungssteuerung.MIN_LAUFZEIT == 21

    def test_kommentare_und_leerzeilen(self, tmp_path):
        pfad = schreibe_ini(tmp_path / "config.ini",
                            "; Kommentar im INI-Stil\n"
                            "# auch Hash\n"
                            "\n"
                            "[Heizungssteuerung]\n"
                            "MIN_PAUSE = 45\n")
        assert ConfigManager(config_path=pfad).get().Heizungssteuerung.MIN_PAUSE == 45
