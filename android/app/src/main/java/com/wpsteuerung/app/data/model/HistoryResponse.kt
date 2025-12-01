package com.wpsteuerung.app.data.model

data class HistoryDataPoint(
    val timestamp: String,
    val temperatures: TemperatureData,
    val compressor: String,
    val setpoints: Setpoints,
    val power_source: String
)

data class HistoryResponse(
    val data: List<HistoryDataPoint>,
    val count: Int
)
