package com.wpsteuerung.app.data.model

import com.google.gson.annotations.SerializedName

data class TemperatureData(
    val oben: Double?,
    val mittig: Double?,
    val unten: Double?,
    val verdampfer: Double?,
    val vorlauf: Double?,                   // NEU
    val boiler: Double?
)

data class CompressorData(
    val status: String = "unknown",
    @SerializedName("runtime_current")
    val runtimeCurrent: String = "0:00:00",
    @SerializedName("runtime_today")
    val runtimeToday: String = "0:00:00",
    @SerializedName("activation_reason")
    val activationReason: String? = null,   // NEU
    @SerializedName("blocking_reason")
    val blockingReason: String? = null      // NEU
)

data class Setpoints(
    val einschaltpunkt: Double?,
    val ausschaltpunkt: Double?,
    @SerializedName("sicherheits_temp")
    val sicherheitsTemp: Double?,
    val verdampfertemperatur: Double?,
    @SerializedName("active_sensor")
    val activeSensor: String? = null        // NEU
)

data class ModeData(
    val current: String = "unknown",
    @SerializedName("solar_active")
    val solarActive: Boolean = false,
    @SerializedName("holiday_active")
    val holidayActive: Boolean = false,
    @SerializedName("bath_active")
    val bathActive: Boolean = false
)

data class EnergyData(
    @SerializedName("battery_power")
    val batteryPower: Double?,              // FIX: Int → Double (kann negativ sein)
    val soc: Double?,                       // FIX: Int → Double
    @SerializedName("feed_in")
    val feedIn: Double?,                    // FIX: Int → Double
    @SerializedName("pv_power")
    val pvPower: Double? = null,            // NEU
    @SerializedName("battery_capacity_kwh")
    val batteryCapacityKwh: Double? = null  // NEU
)

data class ForecastData(                    // NEU: war komplett im Model
    val today: Double?,
    val tomorrow: Double?,
    val sunrise: String?,
    val sunset: String?
)

data class SystemInfo(
    @SerializedName("exclusion_reason")
    val exclusionReason: String? = null,
    @SerializedName("last_update")
    val lastUpdate: String = "",
    // FIX: mode existiert nicht in der API → entfernt
    @SerializedName("vpn_ip")
    val vpnIp: String? = null               // NEU
)

data class SystemStatus(
    val temperatures: TemperatureData,
    val compressor: CompressorData,
    val setpoints: Setpoints,
    val mode: ModeData,
    val energy: EnergyData,
    val system: SystemInfo,
    val forecast: ForecastData? = null      // NEU
)