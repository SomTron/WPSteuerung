package com.wpsteuerung.app.data.api

import com.wpsteuerung.app.BuildConfig
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

object RetrofitClient {

    private const val BASE_URL = "http://10.100.0.1:8000/"

    // FIX: API-Key aus BuildConfig statt hardcoded im Source-Code
    // → in local.properties: api_key=QRzq8MwDDHP3NZjx
    // → in build.gradle.kts: buildConfigField("String", "API_KEY", "\"${properties["api_key"]}\"")
    private val API_KEY = BuildConfig.API_KEY

    private val authInterceptor = Interceptor { chain ->
        val request = chain.request().newBuilder()
            .addHeader("X-API-Key", API_KEY)
            .build()
        chain.proceed(request)
    }

    // FIX: Logging nur in Debug-Builds — sonst werden API-Key und
    // alle Response-Bodies im Klartext geloggt
    private val loggingInterceptor = HttpLoggingInterceptor().apply {
        level = if (BuildConfig.DEBUG) HttpLoggingInterceptor.Level.BODY
        else HttpLoggingInterceptor.Level.NONE
    }

    private val okHttpClient = OkHttpClient.Builder()
        .addInterceptor(authInterceptor)
        .addInterceptor(loggingInterceptor)
        // FIX: 30s statt 15s — robuster auf schlechten mobilen Verbindungen
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    private val retrofit = Retrofit.Builder()
        .baseUrl(BASE_URL)
        .client(okHttpClient)
        .addConverterFactory(GsonConverterFactory.create())
        .build()

    val apiService: WPApiService = retrofit.create(WPApiService::class.java)
}
