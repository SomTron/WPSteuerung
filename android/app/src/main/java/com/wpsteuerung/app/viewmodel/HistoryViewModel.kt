package com.wpsteuerung.app.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.wpsteuerung.app.data.model.HistoryResponse
import com.wpsteuerung.app.data.repository.WPRepository
import kotlinx.coroutines.flow.MutableState

Flow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

class HistoryViewModel(
    private val repository: WPRepository = WPRepository()
) : ViewModel() {
    
    private val _uiState = MutableStateFlow<HistoryUiState>(HistoryUiState.Loading)
    val uiState: StateFlow<HistoryUiState> = _uiState.asStateFlow()
    
    init {
        loadHistory()
    }
    
    fun loadHistory(hours: Int = 6) {
        viewModelScope.launch {
            _uiState.value = HistoryUiState.Loading
            repository.getHistory(hours = hours, limit = 100).fold(
                onSuccess = { history ->
                    _uiState.value = HistoryUiState.Success(history)
                },
                onFailure = { error ->
                    _uiState.value = HistoryUiState.Error(error.message ?: "Unknown error")
                }
            )
        }
    }
}

sealed class HistoryUiState {
    object Loading : HistoryUiState()
    data class Success(val history: HistoryResponse) : HistoryUiState()
    data class Error(val message: String) : HistoryUiState()
}
