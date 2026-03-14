package com.wpsteuerung.app.util

import java.time.LocalDateTime
import java.time.format.DateTimeFormatter

object DateUtils {
    private val inputFormatter   = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")
    private val displayFormatter = DateTimeFormatter.ofPattern("HH:mm:ss")
    private val axisFormatter    = DateTimeFormatter.ofPattern("dd.MM HH:mm")

    /**
     * Parst robust: "2026-03-14 21:36:33" (API-Format mit Leerzeichen).
     * take(19) entfernt eventuelle Millisekunden-Suffixe.
     */
    private fun parseLocalDateTime(timestamp: String): LocalDateTime =
        LocalDateTime.parse(timestamp.take(19), inputFormatter)

    fun formatTimestamp(timestamp: String): String = runCatching {
        parseLocalDateTime(timestamp).format(displayFormatter)
    }.getOrDefault(timestamp)

    fun formatTimestampForAxis(timestamp: String): String = runCatching {
        parseLocalDateTime(timestamp).format(axisFormatter)
    }.getOrDefault("")

    /**
     * Konvertiert einen Timestamp in Minuten seit der Unix-Epoche.
     * Wird als X-Wert für das Vico-Diagramm verwendet.
     */
    fun parseToMinutes(timestamp: String): Long = runCatching {
        parseLocalDateTime(timestamp).toEpochSecond(java.time.ZoneOffset.UTC) / 60L
    }.getOrElse {
        android.util.Log.e("DateUtils", "parseToMinutes fehlgeschlagen für: '$timestamp'", it)
        0L
    }

    /**
     * Rechnet einen Minuten-Offset (X-Achsenwert von Vico) zurück in einen
     * lesbaren Zeitstempel.
     */
    fun formatMinutesOffset(startMinutes: Long, offsetMinutes: Long): String = runCatching {
        val totalMinutes = startMinutes + offsetMinutes
        java.time.Instant.ofEpochSecond(totalMinutes * 60L)
            .atOffset(java.time.ZoneOffset.UTC)
            .toLocalDateTime()
            .format(axisFormatter)
    }.getOrDefault("")
}
