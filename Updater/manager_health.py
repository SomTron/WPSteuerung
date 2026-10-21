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
    """Formatiert den Analyse-Qualitaetsbericht. Rueckgabe: (Text, gruen)."""
    level = str(report.get("quality_level", report.get("status", "unknown")))
    gruen = level in {"ok", "gruen", "good", "green"}
    zeilen = ["=== Analyse-Qualitaet ===", f"Stufe:        {level}"]
    zeilen.append(f"Git-Revision: {_fmt(report.get('git_revision'))}")
    erzeugt = report.get("erzeugt_am") or report.get("generated_at")
    if erzeugt:
        zeilen.append(f"Erzeugt:      {erzeugt}")
    zeilen.append(f"Schema:       {_fmt(report.get('schema'))}")

    zyklen = report.get("zyklen")
    if isinstance(zyklen, dict):
        zeilen.append("")
        zeilen.append("Zyklen:       " + _kennzahlen_zeile(zyklen))
    entscheidungen = report.get("entscheidungen")
    if isinstance(entscheidungen, dict):
        zeilen.append("Entscheidungen: " + _kennzahlen_zeile(entscheidungen))

    szenarien = report.get("szenarien")
    if isinstance(szenarien, dict) and szenarien:
        zeilen.append("")
        zeilen.append("Szenarien:")
        for name in sorted(szenarien):
            zeilen.append(f"  - {name}: {_kennzahlen_zeile(szenarien[name])}")

    probleme = report.get("probleme")
    if isinstance(probleme, list) and probleme:
        zeilen.append("")
        zeilen.append(f"Probleme ({len(probleme)}):")
        for eintrag in probleme[:10]:
            zeilen.append(f"  - {_fmt(eintrag)}")
        if len(probleme) > 10:
            zeilen.append(f"  ... {len(probleme) - 10} weitere")
    return "\n".join(zeilen), gruen


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
