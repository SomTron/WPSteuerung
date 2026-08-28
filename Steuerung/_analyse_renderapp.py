# -*- coding: utf-8 -*-
"""Analysiert renderApp() in webapp/index.html auf gefaehrliche Interpolationen."""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

t = open("webapp/index.html", encoding="utf-8").read()
start = t.find("function renderApp")
ende = t.find("function toggleMode", start)
seg = t[start:ende]
print(f"renderApp: {start}..{ende}, {len(seg)} Zeichen")

# Alle ${ ... } inkl. verschachtelter Klammern finden
pattern = re.compile(r"\$\{((?:[^{}]|\{[^{}]*\})*)\}")
exprs = pattern.findall(seg)
print("Anzahl Interpolationen:", len(exprs))
for e in exprs:
    e_show = e.strip().replace("\n", " ")
    if len(e_show) > 130:
        e_show = e_show[:130] + " ..."
    print("  ${", e_show, "}")

# Pruefe: Zugriffsketten ohne Schutz (?.) auf statusData-Felder
risiko = [e for e in exprs if re.search(r"\.\w+\.", e) and "?." not in e]
print("\nMoeglicherweise ungeschuetzte Ketten:")
for e in risiko:
    print("  $", e.strip()[:150])
