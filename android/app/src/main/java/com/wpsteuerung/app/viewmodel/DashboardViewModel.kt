package com.wpsteuerung.app.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.wpsteuerung.app.data.model.SystemStatus
import com.wpsteuerung.app.data.repository.WPRepository
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

class DashboardViewModel(
    private val repository: WPRepository = WPRepository()
) : ViewModel() {

    private val _uiState = MutableStateFlow<DashboardUiState>(DashboardUiState.Loading)
    val uiState: StateFlow<DashboardUiState> = _uiState.asStateFlow()

    private val _isRefreshing = MutableStateFlow(false)
    val isRefreshing: StateFlow<Boolean> = _isRefreshing.asStateFlow()

    private val _controlError = MutableStateFlow<String?>(null)
    val controlError: StateFlow<String?> = _controlError.asStateFlow()

    // FIX: Job-Handle verhindert gleichzeitige loadStatus()-Calls
    private var statusJob: Job? = null
    // FIX: private — macht von außen keinen Sinn aufzurufen
    private var refreshJob: Job? = null

    init {
        loadStatus()
        startAutoRefresh()
    }

    fun loadStatus() {
        // FIX: Laufenden Job abbrechen bevor neuer gestartet wird →
        // verhindert Race Conditions bei schnellen Klicks / Auto-Refresh
        statusJob?.cancel()
        statusJob = viewModelScope.launch {
            _isRefreshing.value = true
            repository.getStatus().fold(
                onSuccess = { status ->
                    _uiState.value = DashboardUiState.Success(status)
                    _isRefreshing.value = false
                },
                onFailure = { error ->
                    _uiState.value = DashboardUiState.Error(
                        error.message ?: "Unbekannter Fehler"
                    )
                    _isRefreshing.value = false
                }
            )
        }
    }

    fun toggleBademodus() {
        viewModelScope.launch {
            val currentState = (_uiState.value as? DashboardUiState.Success)
                ?.status?.mode?.bathActive ?: return@launch
            repository.setBademodus(!currentState).fold(
                onSuccess = { loadStatus() },
                onFailure = { error ->
                    _controlError.value = "Bademodus konnte nicht geändert werden: ${error.message}"
                }
            )
        }
    }

    fun setUrlaubsmodus(enabled: Boolean, durationHours: Int? = null) {
        viewModelScope.launch {
            repository.setUrlaubsmodus(enabled, durationHours).fold(
                onSuccess = { loadStatus() },
                onFailure = { error ->
                    _controlError.value = "Urlaubsmodus konnte nicht geändert werden: ${error.message}"
                }
            )
        }
    }

    fun clearControlError() {
        _controlError.value = null
    }

    private fun startAutoRefresh() {
        refreshJob?.cancel()
        refreshJob = viewModelScope.launch {
            while (true) {
                delay(5_000)
                loadStatus()
            }
        }
    }
}

sealed interface DashboardUiState {
    data object Loading : DashboardUiState
    data class Success(val status: SystemStatus) : DashboardUiState
    data class Error(val message: String) : DashboardUiState
}