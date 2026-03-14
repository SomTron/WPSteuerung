package com.wpsteuerung.app.data.model

import com.google.gson.annotations.SerializedName

data class HistoryDataPoint(
    val timestamp: String,
    @SerializedName("t_oben")
    val tOben: Double?,
    @SerializedName("t_mittig")
    val tMittig: Double?,
    @SerializedName("t_unten")
    val tUnten: Double?,
    @SerializedName("t_verd")
    val tVerd: Double?,
    @SerializedName("t_vorlauf")
    val tVorlauf: Double? = null,           // NEU
    @SerializedName("t_boiler")
    val tBoiler: Double? = null,            // NEU
    val kompressor: String = "unknown",     // FIX: Default damit Gson nicht crasht
    @SerializedName("setpoint_on")
    val setpointOn: Double? = null,
    @SerializedName("setpoint_off")
    val setpointOff: Double? = null
)

data class HistoryResponse(
    val data: List<HistoryDataPoint>,
    val count: Int
)