package com.wpsteuerung.app.data.api

import com.wpsteuerung.app.data.local.Einstellungen
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

object RetrofitClient {
    
    // Basis-URL aus den Einstellungen. Ist keine gesetzt (erster Start),
    // faellt auf den Emulator-Default zurueck, damit Entwicklung weiter
    // funktioniert. Fuer echte Geraete die URL in den Einstellungen eintragen
    // (IP oder Tailscale-URL, z. B. https://hostname.ts.net).
    private val baseUrl: String
        get() = Einstellungen.baseUrl.ifBlank { "http://10.0.2.2:5000/" }
    
    private val loggingInterceptor = HttpLoggingInterceptor().apply {
        // HEADERS statt BODY, um die Antwortkoerper nicht vollstaendig zu
        // protokollieren. Wichtig: HEADERS allein schuetzt den Schluessel
        // NICHT - er stuende als X-API-Key im Klartext drin. redactHeader
        // ersetzt ihn durch Blockzeichen; das Log bleibt trotzdem lesbar.
        level = HttpLoggingInterceptor.Level.HEADERS
        redactHeader("X-API-Key")
    }

    /**
     * Haengt den Schluessel an jeden Aufruf.
     *
     * Die Leserouten sind serverseitig offen, die Schreibrouten nicht
     * (api.py::_check_api_key). Wie in der WebApp wird er grundsaetzlich
     * mitgeschickt, damit die App auch dann funktioniert, wenn eine
     * Leseroute spaeter nachgezogen wird.
     */
    private val apiKeyInterceptor = Interceptor { chain ->
        val key = Einstellungen.apiKey
        val request = chain.request().newBuilder()
            .apply { if (key.isNotBlank()) header("X-API-Key", key) }
            .build()
        chain.proceed(request)
    }
    
    private val okHttpClient = OkHttpClient.Builder()
        .addInterceptor(apiKeyInterceptor)
        .addInterceptor(loggingInterceptor)
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .build()
    
    val apiService: WPApiService by lazy {
        Retrofit.Builder()
            .baseUrl(baseUrl)
            .client(okHttpClient)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(WPApiService::class.java)
    }
}
