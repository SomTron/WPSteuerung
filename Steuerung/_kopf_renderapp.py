# -*- coding: utf-8 -*-
"""Gibt den Kopf von renderApp() aus webapp/index.html aus."""
import sys

sys.stdout.reconfigure(encoding="utf-8")

t = open("webapp/index.html", encoding="utf-8").read()
start = t.find("function renderApp")
print(t[start:start + 3200])
