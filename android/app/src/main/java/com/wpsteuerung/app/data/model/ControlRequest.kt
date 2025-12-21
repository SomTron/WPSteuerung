package com.wpsteuerung.app.data.model

data class ControlRequest(
    val action: String,
    val enabled: Boolean? = null,
    val duration_hours: Int? = null
)

data class ControlResponse(
    val success: Boolean,
    val message: String,
    val new_state: Map<String, Any?>
)
