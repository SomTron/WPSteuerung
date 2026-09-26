"""Auswertung der Simulationsergebnisse (KPIs, Tagesuebersicht, Zyklen)."""
from __future__ import annotations

import statistics as st
from collections import defaultdict
from typing import Dict


def tageswerte(sim) -> Dict:
    """Aggregiert die Zeitreihe zu Tageswerten."""
    pro_tag: Dict = defaultdict(lambda: dict(
        netz_wh=0.0, pv_wh=0.0, einsp_wh=0.0, lauf_s=0.0,
        min_unten=99.0, max_unten=0.0, min_oben=99.0, max_oben=0.0,
        min_mitte=99.0, n_punkte=0, regeln=defaultdict(float),
    ))
    dt_h = (sim.zeitreihe[1]["t"] - sim.zeitreihe[0]["t"]).total_seconds() / 3600.0 if len(sim.zeitreihe) > 1 else 0.033
    for r in sim.zeitreihe:
        d = pro_tag[r["t"].date()]
        d["netz_wh"] += max(0.0, r["netz"]) * dt_h
        d["pv_wh"] += max(0.0, r["pv"]) * dt_h
        d["einsp_wh"] += max(0.0, r["einspeisung"]) * dt_h
        if r["k"]:
            d["lauf_s"] += dt_h * 3600
        d["min_unten"] = min(d["min_unten"], r["unten"])
        d["max_unten"] = max(d["max_unten"], r["unten"])
        d["min_oben"] = min(d["min_oben"], r["oben"])
        d["max_oben"] = max(d["max_oben"], r["oben"])
        d["min_mitte"] = min(d["min_mitte"], r["mitte"])
        d["n_punkte"] += 1
        d["regeln"][r["regel"] or "-"] += dt_h * 3600
    return dict(pro_tag)


def kpi(sim) -> dict:
    """Die Kennzahlen eines Simulationslaufs."""
    tage = sim.sz.tage
    zyklen = sim.zyklen
    dauer = [z.dauer_min for z in zyklen]
    pv_anteil = (sim.pv_eigenverbraucht_wh / sim.pv_erzeugt_wh * 100.0) if sim.pv_erzeugt_wh > 1 else 0.0
    wp_pv = sum(z.pv_wh for z in zyklen)
    wp_netz = sum(z.netz_wh for z in zyklen)
    return {
        "tage": tage,
        "pv_erzeugt_kwh": sim.pv_erzeugt_wh / 1000.0,
        "pv_eigenverbraucht_kwh": sim.pv_eigenverbraucht_wh / 1000.0,
        "pv_eingespeist_kwh": sim.pv_eingespeist_wh / 1000.0,
        "pv_eigenverbrauch_prozent": pv_anteil,
        "netzbezug_kwh": sim.netzbezug_wh / 1000.0,
        "netz_pro_tag_kwh": sim.netzbezug_wh / 1000.0 / max(tage, 1),
        "wp_energie_kwh": sim.wp_energie_wh / 1000.0,
        "wp_laufzeit_h": sim.wp_laufzeit_s / 3600.0,
        "wp_auslastung_prozent": sim.wp_laufzeit_s / (tage * 86400.0) * 100.0,
        "zyklen": len(zyklen),
        "zyklen_pro_tag": len(zyklen) / max(tage, 1),
        "zyklus_dauer_median": st.median(dauer) if dauer else 0.0,
        "kurzlaeufe": sum(1 for d in dauer if d < 20),
        "kurzlaeufe_prozent": (sum(1 for d in dauer if d < 20) / len(dauer) * 100.0) if dauer else 0.0,
        "langlaeufe": sum(1 for d in dauer if d > 180),
        "boiler_max_ereignisse": sim.boiler_max_ereignisse,
        "kalt_wasser_tage": sim.kalt_wasser_tage,
        "wp_ pv_kwh": wp_pv / 1000.0,
        "wp_netz_kwh": wp_netz / 1000.0,
        "wp_netzanteil_prozent": (wp_netz / (wp_netz + wp_pv) * 100.0) if (wp_netz + wp_pv) > 1 else 0.0,
        "legionellen_fertig": sim.legionellen_fertig,
        "waerme_zapf_kwh": sim.waerme_zapf_wh / 1000.0,
    }


def regelverteilung(sim) -> Dict[str, float]:
    """Laufzeit je ausloesender Regel in Stunden."""
    erg: Dict[str, float] = defaultdict(float)
    for z in sim.zyklen:
        erg[z.regel or "?"] += z.dauer_min / 60.0
    return dict(sorted(erg.items(), key=lambda kv: -kv[1]))


def bericht(sim, titel: str = "") -> str:
    """Formatiert einen kompakten Ergebnisbericht."""
    k = kpi(sim)
    t = tageswerte(sim)
    linie = "=" * 78
    out = [linie, f"{titel or sim.sz.name}", linie]
    out.append(f"Zeitraum           : {sim.zeitreihe[0]['t']:%d.%m.%Y} - "
               f"{sim.zeitreihe[-1]['t']:%d.%m.%Y} ({k['tage']} Tage)")
    out.append(f"PV-Erzeugung       : {k['pv_erzeugt_kwh']:.2f} kWh "
               f"({k['pv_erzeugt_kwh']/max(k['tage'],1):.2f} kWh/Tag)")
    out.append(f"PV-Eigenverbrauch  : {k['pv_eigenverbraucht_kwh']:.2f} kWh "
               f"= {k['pv_eigenverbrauch_prozent']:.0f}% der Erzeugung")
    out.append(f"Netzbezug          : {k['netzbezug_kwh']:.2f} kWh "
               f"({k['netz_pro_tag_kwh']:.2f} kWh/Tag)")
    out.append(f"WP-Laufzeit        : {k['wp_laufzeit_h']:.1f} h "
               f"({k['wp_auslastung_prozent']:.0f}% der Zeit, {k['wp_energie_kwh']:.2f} kWh)")
    out.append(f"WP-Stromquelle     : {k['wp_netzanteil_prozent']:.0f}% Netz / "
               f"{100-k['wp_netzanteil_prozent']:.0f}% PV+Batterie")
    out.append(f"Zyklen             : {k['zyklen']} ({k['zyklen_pro_tag']:.1f}/Tag), "
               f"Median {k['zyklus_dauer_median']:.0f} min")
    out.append(f"  Kurzlaeufe <20min: {k['kurzlaeufe']} ({k['kurzlaeufe_prozent']:.0f}%) | "
               f"Langlaeufe >180min: {k['langlaeufe']}")
    out.append(f"Boiler-Max-Ausloes.: {k['boiler_max_ereignisse']}")
    out.append(f"Legionellenlauf    : {'abgeschlossen' if k['legionellen_fertig'] else 'NICHT abgeschlossen'}")
    out.append(f"Tage mit unten<40C : {k['kalt_wasser_tage']} von {k['tage']}")
    out.append("")
    out.append("Tagesuebersicht (unten = untererFuehler, kaltester Punkt)")
    out.append("  Datum        PV kWh  Netz kWh  Lauf h  min unten  max unten  max oben  min mitte")
    for d in sorted(t):
        v = t[d]
        out.append(f"  {d.strftime('%d.%m.%Y'):<12} {v['pv_wh']/1000:6.2f}  "
                   f"{v['netz_wh']/1000:8.2f}  {v['lauf_s']/3600:6.2f}  "
                   f"{v['min_unten']:9.1f}  {v['max_unten']:9.1f}  "
                   f"{v['max_oben']:8.1f}  {v['min_mitte']:9.1f}")
    out.append("")
    out.append("Laufzeit je ausloesender Regel")
    for name, h in regelverteilung(sim).items():
        out.append(f"  {name:<24} {h:6.2f} h")
    out.append("")
    out.append("Diagnose: Sperrgruende, wenn eine Regel einschalten wollte")
    if sim.diag_blockiert_h:
        for grund, h in sorted(sim.diag_blockiert_h.items(), key=lambda kv: -kv[1])[:8]:
            out.append(f"  {grund:<44} {h:6.2f} h")
    else:
        out.append("  (keine)")
    out.append(f"  {'WP mit Netzstrom (von der Laufzeit)':<44} "
               f"{sim.diag_netz_waermer_h:6.2f} h")
    out.append(f"  {'PV >= 1kW, WP aus (vergebliche PV)':<44} "
               f"{sim.diag_pv_vergeblich_wh/1000:6.2f} kWh in "
               f"{sim.diag_pv_vergeblich_ohne_wunsch_h:5.1f} h")
    if getattr(sim, "legionellen_hinweis", ""):
        out.append(f"  Legionellen-Plan: {sim.legionellen_hinweis}")
    out.append(linie)
    return "\n".join(out)
