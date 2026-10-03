package com.wpsteuerung.app.data.model

import com.google.gson.annotations.SerializedName

// Temperature data from API
data class TemperatureData(
    val oben: Double?,
    val mittig: Double?,
    val unten: Double?,
    val verdampfer: Double?,
    val boiler: Double?
)

// Compressor data from API
data class CompressorData(
    val status: String,
    @SerializedName("runtime_current")
    val runtimeCurrent: String,
    @SerializedName("runtime_today")
    val runtimeToday: String
)

// Setpoints from API
data class Setpoints(
    val einschaltpunkt: Double?,
    val ausschaltpunkt: Double?,
    @SerializedName("sicherheits_temp")
    val sicherheitsTemp: Double?,
    val verdampfertemperatur: Double?
)

// Mode data from API
data class ModeData(
    val current: String,
    @SerializedName("solar_active")
    val solarActive: Boolean,
    @SerializedName("holiday_active")
    val holidayActive: Boolean,
    @SerializedName("bath_active")
    val bathActive: Boolean
)

// Energy data from API
data class EnergyData(
    @SerializedName("battery_power")
    val batteryPower: Int,
    val soc: Int,
    @SerializedName("feed_in")
    val feedIn: Int
)

// System info from API
data class SystemInfo(
    @SerializedName("exclusion_reason")
    val exclusionReason: String?,
    @SerializedName("last_update")
    val lastUpdate: String,
    val mode: String
)

// Main system status matching backend API structure
data class SystemStatus(
    val temperatures: TemperatureData,
    val compressor: CompressorData,
    val setpoints: Setpoints,
    val mode: ModeData,
    val energy: EnergyData,
    val system: SystemInfo,
    // Not-Aus-Sperre. Die Felder kommen FLACH aus /status (api.py), nicht aus
    // einem Unterobjekt - deshalb stehen sie hier direkt am Wurzelobjekt.
    //
    // Wichtig: die Strings sind nullable. SystemStatus hat keinen No-Arg-
    // Konstruktor, also erzeugt Gson die Instanz per Unsafe und setzt nur
    // die tatsaechlich vorhandenen Felder. Ein Kotlin-Standardwert wie
    // notausTs = "" gilt dann NICHT, das Feld bliebe null und waere als
    // nicht-null String deklariert - ein Absturz beim Zugriff. Boolean ist
    // unkritisch, dessen JVM-Standard ist bereits false.
    @SerializedName("notaus_aktiv")
    val notausAktiv: Boolean = false,
    @SerializedName("notaus_grund")
    val notausGrund: String? = null,
    @SerializedName("notaus_ts")
    val notausTs: String? = null
)
