package com.wpsteuerung.app.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.wpsteuerung.app.data.model.HistoryResponse
import com.wpsteuerung.app.data.repository.WPRepository
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

class HistoryViewModel(
    private val repository: WPRepository = WPRepository()
) : ViewModel() {

    private val _uiState = MutableStateFlow<HistoryUiState>(HistoryUiState.Loading)
    val uiState: StateFlow<HistoryUiState> = _uiState.asStateFlow()

    private val _selectedHours = MutableStateFlow(6)
    val selectedHours: StateFlow<Int> = _selectedHours.asStateFlow()

    // FIX: Job-Handle verhindert gleichzeitige Calls
    private var historyJob: Job? = null

    init {
        loadHistory()
    }

    fun loadHistory(hours: Int = _selectedHours.value) {
        _selectedHours.value = hours

        // FIX: Dynamisches Limit — ~1 Punkt alle 5 Minuten statt pauschal 100
        // Bei 6h → 72, bei 24h → 288 Punkte
        val limit = hours * 12

        historyJob?.cancel()
        historyJob = viewModelScope.launch {
            _uiState.value = HistoryUiState.Loading
            repository.getHistory(hours = hours, limit = limit).fold(
                onSuccess = { history ->
                    _uiState.value = HistoryUiState.Success(history)
                },
                onFailure = { error ->
                    _uiState.value = HistoryUiState.Error(
                        error.message ?: "Unbekannter Fehler"
                    )
                }
            )
        }
    }

    fun retry() = loadHistory()
}

sealed interface HistoryUiState {
    data object Loading : HistoryUiState
    data class Success(val history: HistoryResponse) : HistoryUiState
    data class Error(val message: String) : HistoryUiState
}