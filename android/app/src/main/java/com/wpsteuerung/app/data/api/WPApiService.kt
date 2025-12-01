package com.wpsteuerung.app.data.api

import com.wpsteuerung.app.data.model.ControlRequest
import com.wpsteuerung.app.data.model.ControlResponse
import com.wpsteuerung.app.data.model.HistoryResponse
import com.wpsteuerung.app.data.model.SystemStatus
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Query

interface WPApiService {
    
    @GET("status")
    suspend fun getStatus(): SystemStatus
    
    @GET("history")
    suspend fun getHistory(
        @Query("hours") hours: Int = 6,
        @Query("limit") limit: Int = 100
    ): HistoryResponse
    
    @POST("control")
    suspend fun control(@Body request: ControlRequest): ControlResponse
}
