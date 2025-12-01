package com.wpsteuerung.app.data.model

data class TemperatureData(
    val oben: Double?,
    val mittig: Double?,
    val unten: Double?,
    val verdampfer: Double?
)

data class ModeData(
    val solar_excess: Boolean,
    val night_reduction: Double,
    val setpoints: Setpoints
)

data class Setpoints(
    val on: Double?,
   val off: Double?
)

data class SystemStatus(
    val temperatures: TemperatureData,
    val compressor: String,
    val power_source: String?,
    val current_runtime: String?,
    val last_runtime: String?,
    val total_runtime_today: String,
    val mode: ModeData
)
