package com.wpsteuerung.app.data.model

import com.google.gson.annotations.SerializedName

// Control request matching backend API expectations
data class ControlRequest(
    // muss in ALLOWED_COMMANDS der API stehen, sonst HTTP 422:
    // "set_mode", "force_on", "force_off", "notaus", "notaus_aus"
    val command: String,
    val params: Map<String, Any>? = null
)

// Helper function to create mode control requests
fun createModeRequest(mode: String, active: Boolean): ControlRequest {
    return ControlRequest(
        command = "set_mode",
        params = mapOf(
            "mode" to mode,
            "active" to active
        )
    )
}

// Control response from backend
data class ControlResponse(
    val status: String,
    val message: String
)
