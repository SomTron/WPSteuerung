package com.wpsteuerung.app.data.api

import com.wpsteuerung.app.data.local.Einstellungen
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

object RetrofitClient {
    
    // Local test server (api_server.py)
    private const val BASE_URL = "http://10.0.2.2:5000/"  // Android Emulator -> localhost
    // For physical device on same network, use: "http://YOUR_PC_IP:5000/"
    
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
    
    private val retrofit = Retrofit.Builder()
        .baseUrl(BASE_URL)
        .client(okHttpClient)
        .addConverterFactory(GsonConverterFactory.create())
        .build()
    
    val apiService: WPApiService = retrofit.create(WPApiService::class.java)
}
