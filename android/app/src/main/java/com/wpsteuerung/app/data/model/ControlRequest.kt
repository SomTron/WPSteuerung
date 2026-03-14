package com.wpsteuerung.app.data.model

import com.google.gson.JsonObject

// FIX: Map<String, Any> durch JsonObject ersetzt — Gson serialisiert Any
// (besonders Boolean) manchmal falsch. JsonObject ist typsicher und zuverlässig.
data class ControlRequest(
    val command: String,
    val params: JsonObject? = null
)

// FIX: Default-Werte gegen Gson-Crashes bei fehlendem Feld
data class ControlResponse(
    val status: String = "",
    val message: String = ""
)

// ─────────────────────────────────────────────
// Hilfsfunktionen
// ─────────────────────────────────────────────

fun createModeRequest(mode: String, active: Boolean): ControlRequest =
    ControlRequest(
        command = "set_mode",
        params = JsonObject().apply {
            addProperty("mode", mode)
            addProperty("active", active)
        }
    )

fun createUrlaubsmodusRequest(enabled: Boolean, durationHours: Int?): ControlRequest =
    ControlRequest(
        command = "set_mode",
        params = JsonObject().apply {
            addProperty("mode", "urlaubsmodus")
            addProperty("active", enabled)
            durationHours?.let { addProperty("duration_hours", it) }
        }
    )