package com.wpsteuerung.app.data.model

import com.google.gson.annotations.SerializedName

// History data point from backend API
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
    val kompressor: String
)

// History response from backend
data class HistoryResponse(
    val data: List<HistoryDataPoint>,
    val count: Int
)
