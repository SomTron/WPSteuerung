package com.wpsteuerung.app.data.local

import android.content.Context
import android.content.SharedPreferences

/**
 * Dauerhafte Ablage der Verbindungseinstellungen.
 *
 * Wichtig fuer den API-Schluessel: `/control` und die uebrigen
 * Schreibrouten verlangen ihn serverseitig, sobald WPS_API_KEY am Pi
 * gesetzt ist (api.py::_check_api_key). Ohne ihn arbeitet die App nur
 * lesend - jede Schreiboperation endet mit HTTP 401.
 */
object Einstellungen {

    private const val DATEI = "wpsteuerung"
    private const val SCHLUESSEL_API_KEY = "api_key"

    @Volatile
    private var prefs: SharedPreferences? = null

    /** Einmalig aus Application.onCreate() aufrufen. */
    fun init(context: Context) {
        if (prefs != null) return
        synchronized(this) {
            if (prefs == null) {
                prefs = context.applicationContext
                    .getSharedPreferences(DATEI, Context.MODE_PRIVATE)
            }
        }
    }

    var apiKey: String
        get() = prefs?.getString(SCHLUESSEL_API_KEY, "").orEmpty()
        set(wert) {
            prefs?.edit()?.putString(SCHLUESSEL_API_KEY, wert.trim())?.apply()
        }

    /** Ohne diesen Schalter sind alle Schreibbefehle der App wirkungslos. */
    val apiKeyGesetzt: Boolean get() = apiKey.isNotBlank()

    /** Nur zum Testen und fuer den Neustart der Verbindung. */
    fun zuruecksetzen() {
        prefs?.edit()?.clear()?.apply()
    }
}