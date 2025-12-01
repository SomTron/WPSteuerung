package com.wpsteuerung.app.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.wpsteuerung.app.data.model.SystemStatus
import com.wpsteuerung.app.data.repository.WPRepository
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
    
    private val _isBademodus = MutableStateFlow(false)
    val isBademodus: StateFlow<Boolean> = _isBademodus.asStateFlow()
    
    private val _isRefreshing = MutableStateFlow(false)
    val isRefreshing: StateFlow<Boolean> = _isRefreshing.asStateFlow()
    
    init {
        loadStatus()
        startAutoRefresh()
    }
    
    fun loadStatus() {
        viewModelScope.launch {
            _isRefreshing.value = true
            repository.getStatus().fold(
                onSuccess = { status ->
                    _uiState.value = DashboardUiState.Success(status)
                    _isRefreshing.value = false
                },
                onFailure = { error ->
                    _uiState.value = DashboardUiState.Error(error.message ?: "Unknown error")
                    _isRefreshing.value = false
                }
            )
        }
    }
    
    fun toggleBademodus() {
        viewModelScope.launch {
            val newState = !_isBademodus.value
            repository.setBademodus(newState).fold(
                onSuccess = {
                    _isBademodus.value = newState
                    loadStatus() // Refresh status
                },
                onFailure = { error ->
                    // Could show error toast
                }
            )
        }
    }
    
    private fun startAutoRefresh() {
        viewModelScope.launch {
            while (true) {
                delay(5000) // Refresh every 5 seconds
                loadStatus()
            }
        }
    }
}

sealed class DashboardUiState {
    object Loading : DashboardUiState()
    data class Success(val status: SystemStatus) : DashboardUiState()
    data class Error(val message: String) : DashboardUiState()
}
