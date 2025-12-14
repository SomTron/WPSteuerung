package com.wpsteuerung.app.data.repository

import com.wpsteuerung.app.data.api.RetrofitClient
import com.wpsteuerung.app.data.model.ControlRequest
import com.wpsteuerung.app.data.model.ControlResponse
import com.wpsteuerung.app.data.model.HistoryResponse
import com.wpsteuerung.app.data.model.SystemStatus
import com.wpsteuerung.app.data.model.createModeRequest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class WPRepository {
    
    private val apiService = RetrofitClient.apiService
    
    suspend fun getStatus(): Result<SystemStatus> = withContext(Dispatchers.IO) {
        try {
            val response = apiService.getStatus()
            Result.success(response)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
    
    suspend fun getHistory(hours: Int = 6, limit: Int = 100): Result<HistoryResponse> = withContext(Dispatchers.IO) {
        try {
            val response = apiService.getHistory(hours, limit)
            Result.success(response)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
    
    suspend fun setBademodus(enabled: Boolean): Result<ControlResponse> = withContext(Dispatchers.IO) {
        try {
            val request = createModeRequest("bademodus", enabled)
            val response = apiService.control(request)
            Result.success(response)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
    
    suspend fun setUrlaubsmodus(enabled: Boolean, durationHours: Int? = null): Result<ControlResponse> = withContext(Dispatchers.IO) {
        try {
            val params = mutableMapOf<String, Any>(
                "mode" to "urlaubsmodus",
                "active" to enabled
            )
            durationHours?.let { params["duration_hours"] = it }
            
            val request = ControlRequest(
                command = "set_mode",
                params = params
            )
            val response = apiService.control(request)
            Result.success(response)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
    
    suspend fun forceCompressorOn(): Result<ControlResponse> = withContext(Dispatchers.IO) {
        try {
            val request = ControlRequest(command = "force_on")
            val response = apiService.control(request)
            Result.success(response)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
    
    suspend fun forceCompressorOff(): Result<ControlResponse> = withContext(Dispatchers.IO) {
        try {
            val request = ControlRequest(command = "force_off")
            val response = apiService.control(request)
            Result.success(response)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
}
