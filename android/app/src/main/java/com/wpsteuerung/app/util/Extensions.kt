package com.wpsteuerung.app.util

/** Einheitlicher Kompressor-Status-Check für API-Werte "running", "EIN" und "1" */
fun String.isKompressorRunning() = this == "running" || this == "1" || this == "EIN"
