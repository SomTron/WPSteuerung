#!/usr/bin/env python3
"""Health- und Analyse-Qualitaets-Abfrage fuer den WP-Manager.

Zwei Aufgaben, bewusst getrennt vom Menue-Skript, damit sie testbar bleiben:

1. ``health``  - liest den /health-Endpunkt der laufenden Steuerung
2. ``quality`` - liest den Analyse-Qualitaetsbericht (quality_report.json)

Es wird bewusst nur die Standardbibliothek verwendet: Der Manager laeuft auf
einem Raspberry Pi, auf dem nicht zwingend requests oder pandas installiert
sind. Exit-Codes:

    0 = alles lesbar / gesund
    1 = Fehler (nicht erreichbar, kein API-Key, keine Daten)
    2 = Daten vorhanden, aber Quality ist nicht gruen
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_API_BASE = "http://127.0.0.1:8000"
TIMEOUT_S = 10


def api_base() -> str:
    return (os.environ.get("WPS_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def api_key() -> str:
    return (os.environ.get("WPS_API_KEY") or "").strip()


def fetch_health(base: str | None = None, key: str | None = None) -> dict:
    """Holt /health und gibt das dekodierte JSON zurueck.

    Wirft RuntimeError bei Netzfehlern, HTTP-Fehlern und ungueltigem JSON.
    """
    url = f"{(base or api_base()).rstrip('/')}/health"
    request = urllib.request.Request(url, method="GET")
    effektiver_key = api_key() if key is None else key
    if effektiver_key:
        request.add_header("X-API-Key", effektiver_key)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            rohdaten = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} von {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{url} nicht erreichbar: {exc.reason}") from exc
    except OSError as exc:  # pragma: no cover - defensiv
        raise RuntimeError(f"{url} nicht erreichbar: {exc}") from exc
    try:
        payload = json.loads(rohdaten)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"Ungueltige Antwort von {url}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unerwartetes Format von {url}")
    return payload


def load_quality_report(pfad: str | os.PathLike) -> dict:
    """Liest quality_report.json; wirft RuntimeError bei Problemen."""
    datei = Path(pfad)
    if not datei.is_file():
        raise RuntimeError(f"Qualitaetsbericht nicht gefunden: {datei}")
    try:
        # utf-8-sig: ein BOM (Windows-Editor) darf den Bericht nicht unlesbar machen.
        rohdaten = datei.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Qualitaetsbericht nicht lesbar: {exc}") from exc
    try:
        payload = json.loads(rohdaten)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"Qualitaetsbericht ungueltiges JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Qualitaetsbericht: unerwartetes Format")
    return payload


# --------------------------------------------------------------- Ausgabe


def _fmt(value, standard: str = "n/a") -> str:
    if value is None or value == "":
        return standard
    return str(value)


def render_health(health: dict) -> tuple[str, bool]:
    """Formatiert den Health-Status. Rueckgabe: (Text, gesund)."""
    status = str(health.get("status", "unknown"))
    gesund = status == "ok"
    zeilen = ["=== Steuerungs-Health ===", f"Status:       {status}"]
    # WICHTIG: Die Feldnamen muessen zu denen der API passen. Sie stimmten
    # ueber langem nicht ueberein - "Version: n/a", "Loop-Alter: n/a" usw.
    # Und "Regelfehler: 0" war irrefuehrend: es wurde gar kein Wert
    # gelesen, es stand nur die Vorgabe "0" da.
    zeilen.append(f"Version:      {_fmt(health.get('version'))}")
    zeilen.append(f"Loop-Alter:   {_fmt(health.get('loop_heartbeat_age_s'))} s")
    zeilen.append(f"Regelung:     {_fmt(health.get('last_control_success'))}")
    zeilen.append(f"Sensoren:     {_fmt(health.get('last_sensor_success'))}")
    fehler = health.get("consecutive_control_errors")
    zeilen.append(f"Regelfehler:  {_fmt(fehler, '0')}")
    hardware = health.get("hardware")
    if isinstance(hardware, dict):
        if "kompressor_ein" in hardware:
            zeilen.append(
                f"Kompressor:   {'EIN' if hardware['kompressor_ein'] else 'AUS'}"
            )
        if hardware.get("verifizierungsfehler"):
            zeilen.append(
                f"              {hardware['verifizierungsfehler']} "
                "Kompressor-Verifizierungsfehler"
            )
        for feld, label in (
            ("sensor_fehler_seit", "Sensorfehler seit"),
            ("druck_fehler_seit", "Druckfehler seit"),
        ):
            if hardware.get(feld):
                zeilen.append(f"              {label}: {hardware[feld]}")
    persistenz = health.get("last_state_write_ok")
    if persistenz is not None:
        zeilen.append(f"Snapshot:     {_fmt(persistenz)}")
        if health.get("last_state_write_error"):
            zeilen.append(f"              {health['last_state_write_error']}")
    # SD-Karte: traegt die gesamte Historie (CSV, Entscheidungslog, Snapshot).
    # Ein Ausfall kommt nicht ueraschend - die Karte meldet vorher I/O-Fehler.
    sd = health.get("sd_karte")
    if isinstance(sd, dict) and sd.get("sd_ok") is False:
        gesund = False
        zeilen.append("SD-Karte:     WARNUNG")
        for hinweis in sd.get("hinweise", []):
            zeilen.append(f"              {hinweis}")
    elif isinstance(sd, dict) and sd.get("sd_ok") is True:
        zeilen.append("SD-Karte:     ok")
    if not gesund:
        zeilen.append("")
        if status != "ok":
            zeilen.append("Hinweis: 'degraded' heisst meist ein externer Dienst,")
            zeilen.append("nicht zwingend ein Fehler der Heizungssteuerung.")
        else:
            zeilen.append("Hinweis: Die Steuerung laeuft, aber es gibt einen")
            zeilen.append("Warnhinweis (siehe oben).")
    return "\n".join(zeilen), gesund


def render_quality(report: dict) -> tuple[str, bool]:
    """Formatiert den Analyse-Qualitaetsbericht. Rueckgabe: (Text, gruen).

    Feldnamen kommen vom ERZEUGER (`Analyse/analysis_core.build_quality_report`,
    Schema 1): `status`, `schema_version`, `summary`, `scenarios`, `sources`,
    `end_reasons`, `cycle_quality`, `decision_quality`.

    BEFUND 31.10.2026 (Option 23 zeigte "Schema: n/a" und sonst nichts): Der
    Renderer las deutsche Namen, die der Bericht nie enthielt
    (`quality_level`, `schema`, `zyklen`, `entscheidungen`, `szenarien`,
    `probleme`). Deshalb fehlten Schema und Kennzahlen - und der Hinweis
    "siehe Probleme oben" verwies auf eine Liste, die es nicht gab. Die
    Probleme werden jetzt aus den Zaehlern des Berichts abgeleitet
    (`_probleme_aus_report`).
    """
    level = str(report.get("status", report.get("quality_level", "unknown")))
    gruen = level.strip().lower() in {"ok", "gruen", "good", "green"}
    zeilen = ["=== Analyse-Qualitaet ===", f"Stufe:        {level}"]
    zeilen.append(f"Git-Revision: {_fmt(report.get('git_revision'))}")
    erzeugt = report.get("generated_at") or report.get("erzeugt_am")
    if erzeugt:
        zeilen.append(f"Erzeugt:      {erzeugt}")
    zeilen.append(
        f"Schema:       {_fmt(report.get('schema_version', report.get('schema')))}"
    )

    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    if summary:
        zeilen.append("")
        zeilen.append(
            "Zyklen / Entscheidungen: "
            f"{_fmt(summary.get('cycles'), '0')} Zeilen / "
            f"{_fmt(summary.get('decisions'), '0')} Zeilen"
        )
    else:
        # Fallback fuer aeltere/abweichende Berichte.
        zyklen = report.get("zyklen")
        if isinstance(zyklen, dict):
            zeilen.append("")
            zeilen.append("Zyklen:       " + _kennzahlen_zeile(zyklen))
        entscheidungen = report.get("entscheidungen")
        if isinstance(entscheidungen, dict):
            zeilen.append("Entscheidungen: " + _kennzahlen_zeile(entscheidungen))

    szenarien = report.get("scenarios") or report.get("szenarien")
    if isinstance(szenarien, dict) and szenarien:
        zeilen.append("")
        zeilen.append("Szenarien:")
        for name in sorted(szenarien):
            wert = szenarien[name]
            text = _kennzahlen_zeile(wert) if isinstance(wert, dict) else _fmt(wert)
            zeilen.append(f"  - {name}: {text}")

    hinweise = _hinweise_aus_report(report)
    if hinweise:
        zeilen.append("")
        zeilen.extend(hinweise)

    probleme = _probleme_aus_report(report)
    if probleme:
        zeilen.append("")
        zeilen.append(f"Probleme ({len(probleme)}):")
        for eintrag in probleme[:10]:
            zeilen.append(f"  - {eintrag}")
        if len(probleme) > 10:
            zeilen.append(f"  ... {len(probleme) - 10} weitere")
    elif not gruen:
        zeilen.append("")
        zeilen.append("Hinweis: Der Bericht meldet keine Einzelprobleme. Die")
        zeilen.append("Zaehler stehen in quality_report.json unter summary,")
        zeilen.append("cycle_quality und decision_quality.")
    return "\n".join(zeilen), gruen


#: Zaehler in den quality-Bloecken, die ein Problem beschreiben.
_QUALITY_PROBLEME = (
    ("invalid_rows", "ungueltige Zeilen"),
    ("invalid_timestamps", "ungueltige Zeitstempel"),
    ("duplicate_timestamps", "doppelte Zeitstempel"),
    ("backwards_timestamps", "rueckwaerts laufende Zeitstempel"),
    ("missing_source_at_start", "Zeilen ohne source_at_start"),
)


def _als_zahl(wert) -> int:
    try:
        return int(wert)
    except (TypeError, ValueError):
        return 0


def _probleme_aus_report(report: dict) -> list:
    """Leitet aus den Zaehlern des Berichts konkrete Problemzeilen ab.

    Der Bericht (Schema 1) enthaelt keine fertige Problemliste - er zaehlt
    die Auffaelligkeiten in `summary`, `cycle_quality`, `decision_quality`,
    `end_reasons` und `sources`. Genau diese Zaehler macht der Manager
    sichtbar; nur so stimmt der Satz "siehe Probleme oben".
    """
    probleme: list = []
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}

    ungueltig_in_block = False
    for blockname, titel in (
        ("cycle_quality", "Zyklen"),
        ("decision_quality", "Entscheidungen"),
    ):
        block = report.get(blockname)
        if not isinstance(block, dict):
            continue
        for schluessel, text in _QUALITY_PROBLEME:
            anzahl = _als_zahl(block.get(schluessel))
            if anzahl > 0:
                probleme.append(f"{titel}: {anzahl} {text}")
                if schluessel == "invalid_rows":
                    ungueltig_in_block = True
        grund_qualitaet = block.get("reason_quality")
        if isinstance(grund_qualitaet, dict):
            nicht_zugeordnet = _als_zahl(grund_qualitaet.get("direct_unmapped"))
            if nicht_zugeordnet > 0:
                probleme.append(
                    f"{titel}: {nicht_zugeordnet} nicht zugeordnete reason_codes"
                )
            ohne_code = _als_zahl(grund_qualitaet.get("missing"))
            if ohne_code > 0:
                probleme.append(f"{titel}: {ohne_code} ohne reason_code")
        quellen_qualitaet = block.get("source_quality")
        if isinstance(quellen_qualitaet, dict):
            for name, anzahl in sorted(quellen_qualitaet.items()):
                # Abgeleitete/aufgezeichnete Quellen sind kein Datenfehler -
                # sie stehen als Hinweis unter den Kennzahlen.
                if str(name).strip().lower() in {
                    "direct", "ok", "recorded", "inferred_rule", "inferred_feedin",
                }:
                    continue
                wert = _als_zahl(anzahl)
                if wert > 0:
                    probleme.append(f"{titel}: {wert} Quellen '{name}'")

    unbekannt = _als_zahl(summary.get("unknown_reason_codes"))
    if unbekannt > 0:
        in_entscheidungen = _als_zahl(summary.get("unknown_decision_reason_codes"))
        zusatz = (
            f" (davon {in_entscheidungen} in Entscheidungen)"
            if in_entscheidungen > 0
            else ""
        )
        probleme.append(f"{unbekannt} unbekannte reason_codes{zusatz}")
    fehlend = _als_zahl(summary.get("missing_decision_reason_codes"))
    if fehlend > 0:
        probleme.append(f"{fehlend} Entscheidungen ohne reason_code")
    ungueltig = _als_zahl(summary.get("invalid_rows"))
    if ungueltig > 0 and not ungueltig_in_block:
        probleme.append(f"{ungueltig} ungueltige Zeilen insgesamt")
    ohne_quelle = _als_zahl(summary.get("missing_source_at_start"))
    if ohne_quelle > 0:
        probleme.append(f"{ohne_quelle} Zyklen ohne source_at_start")

    for blockname, titel in (("end_reasons", "Endgrund"), ("sources", "Quelle")):
        block = report.get(blockname)
        if not isinstance(block, dict):
            continue
        for name, anzahl in sorted(block.items()):
            basis = str(name).rsplit(":", 1)[-1].strip().lower()
            if basis not in {"unbekannt", "unknown"}:
                continue
            # BEFUND 31.10.2026: Diese Eintraege sind eine Rohzaehlung ueber
            # Zyklen UND Entscheidungen - inklusive Altbestand (Zeilen vor
            # `reason_code`). Gemeldet wird nur, wenn der praezise Zaehler es
            # bestaetigt; sonst entstuenden Probleme, die keine sind.
            if blockname == "end_reasons":
                if str(name).startswith("decision:"):
                    if _als_zahl(summary.get("missing_decision_reason_codes")) <= 0:
                        continue
                else:
                    zyklen_quality = report.get("cycle_quality")
                    if _als_zahl(
                        zyklen_quality.get("unknown_end_grund")
                        if isinstance(zyklen_quality, dict) else 0
                    ) <= 0:
                        continue
            else:
                if _als_zahl(summary.get("missing_source_at_start")) <= 0:
                    continue
            wert = _als_zahl(anzahl)
            if wert > 0:
                probleme.append(f"{titel} '{name}': {wert}")
    return probleme


def _hinweise_aus_report(report: dict) -> list:
    """Zusatzinformationen, die kein Problem, aber erklaerungsbeduerftig sind.

    Beispiel (31.10.2026): 370 Entscheidungszeilen ohne protokollierte Quelle
    (nur aus der Einspeisung geschaetzt) und 257 Altzeilen aus der Zeit vor
    `reason_code`. Beides gehoert sichtbar in die Anzeige, darf die Stufe aber
    nicht auf "degraded" ziehen.
    """
    hinweise: list = []
    entscheidungen = report.get("decision_quality")
    if isinstance(entscheidungen, dict):
        quellen = entscheidungen.get("source_quality")
        if isinstance(quellen, dict):
            teile = [
                f"{name}={wert}"
                for name, wert in sorted(quellen.items())
                if _als_zahl(wert) > 0
            ]
            if teile:
                hinweise.append("Quellen (Entscheidungen): " + ", ".join(teile))
        gruende = entscheidungen.get("reason_quality")
        legacy = _als_zahl(entscheidungen.get("legacy_missing_reason_codes"))
        if legacy <= 0 and isinstance(gruende, dict):
            legacy = _als_zahl(gruende.get("legacy"))
        if legacy > 0:
            hinweise.append(
                f"Altbestand: {legacy} Zeilen ohne reason_code (Schema < 2)"
            )
    return hinweise


def _kennzahlen_zeile(block) -> str:
    if not isinstance(block, dict):
        return _fmt(block)
    teile = []
    for schluessel in ("rows", "zeilen", "count", "cycles", "entries"):
        if schluessel in block:
            teile.append(f"{schluessel}={block[schluessel]}")
            break
    for schluessel in ("schema", "schema_version"):
        if schluessel in block:
            teile.append(f"schema={block[schluessel]}")
            break
    unbekannte = block.get("unbekannte_codes") or block.get("unknown_codes")
    if isinstance(unbekannte, dict) and unbekannte:
        teile.append("unbekannte_codes=" + ",".join(sorted(unbekannte)[:3]))
    elif isinstance(unbekannte, list) and unbekannte:
        teile.append("unbekannte_codes=" + ",".join(str(u) for u in unbekannte[:3]))
    return " | ".join(teile) if teile else "keine Kennzahlen"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="WP-Manager Health/Qualitaet")
    parser.add_argument("modus", choices=["health", "quality"])
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument(
        "--quality-report",
        default=os.environ.get("WPS_QUALITY_REPORT", ""),
        help="Pfad zu quality_report.json",
    )
    args = parser.parse_args(argv)

    try:
        if args.modus == "health":
            text, gesund = render_health(
                fetch_health(base=args.api_base, key=args.api_key)
            )
        else:
            if not args.quality_report:
                print(
                    "Kein Qualitaetsbericht angegeben.\n"
                    "Pfad per --quality-report oder WPS_QUALITY_REPORT setzen.",
                    file=sys.stderr,
                )
                return 1
            text, gesund = render_quality(load_quality_report(args.quality_report))
    except RuntimeError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1

    print(text)
    return 0 if gesund else 2


if __name__ == "__main__":
    raise SystemExit(main())
