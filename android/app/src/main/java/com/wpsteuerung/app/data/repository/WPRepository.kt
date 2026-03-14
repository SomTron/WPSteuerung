package com.wpsteuerung.app.data.repository

import com.wpsteuerung.app.data.api.RetrofitClient
import com.wpsteuerung.app.data.model.ControlResponse
import com.wpsteuerung.app.data.model.HistoryResponse
import com.wpsteuerung.app.data.model.SystemStatus
import com.wpsteuerung.app.data.model.ControlRequest
import com.wpsteuerung.app.data.model.createModeRequest
import com.wpsteuerung.app.data.model.createUrlaubsmodusRequest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class WPRepository {

    private val apiService = RetrofitClient.apiService

    // FIX: Gemeinsames try/catch-Muster extrahiert → kein Boilerplate mehr
    private suspend fun <T> safeApiCall(call: suspend () -> T): Result<T> =
        withContext(Dispatchers.IO) {
            try {
                Result.success(call())
            } catch (e: Exception) {
                Result.failure(e)
            }
        }

    suspend fun getStatus(): Result<SystemStatus> =
        safeApiCall { apiService.getStatus() }

    suspend fun getHistory(hours: Int = 6, limit: Int = 100): Result<HistoryResponse> =
        safeApiCall { apiService.getHistory(hours, limit) }

    suspend fun setBademodus(enabled: Boolean): Result<ControlResponse> =
        safeApiCall { apiService.control(createModeRequest("bademodus", enabled)) }

    // FIX: Verwendet jetzt createUrlaubsmodusRequest statt manueller Map-Erstellung
    suspend fun setUrlaubsmodus(enabled: Boolean, durationHours: Int? = null): Result<ControlResponse> =
        safeApiCall { apiService.control(createUrlaubsmodusRequest(enabled, durationHours)) }

    suspend fun forceCompressorOn(): Result<ControlResponse> =
        safeApiCall { apiService.control(ControlRequest(command = "force_on")) }

    suspend fun forceCompressorOff(): Result<ControlResponse> =
        safeApiCall { apiService.control(ControlRequest(command = "force_off")) }
}