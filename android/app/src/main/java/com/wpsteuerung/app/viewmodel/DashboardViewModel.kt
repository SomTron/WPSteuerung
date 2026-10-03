package com.wpsteuerung.app.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.wpsteuerung.app.data.local.Einstellungen
import com.wpsteuerung.app.data.model.SystemStatus
import com.wpsteuerung.app.data.repository.WPRepository
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
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

    /** Kurzmeldung nach einem Befehl (erfolgreich oder nicht). */
    private val _befehlFeedback = MutableStateFlow<String?>(null)
    val befehlFeedback: StateFlow<String?> = _befehlFeedback.asStateFlow()

    /** Sicherheitsabfrage vor dem Not-Aus. */
    private val _bestaetigungOffen = MutableStateFlow(false)
    val bestaetigungOffen: StateFlow<Boolean> = _bestaetigungOffen.asStateFlow()

    /**
     * Nach `notaus` laeuft der Dienst nicht mehr. Ohne diese Sperre schlaegt
     * jede weitere Abfrage fehl und die Anzeige springt auf die Fehlerseite -
     * der Nutzer haelt den Not-Aus dann fuer wirkungslos.
     */
    private val _dienstBeendet = MutableStateFlow(false)
    val dienstBeendet: StateFlow<Boolean> = _dienstBeendet.asStateFlow()

    init {
        loadStatus()
        startAutoRefresh()
    }

    /** Eingabefeld fuer den API-Schluessel; haelt den ungesicherten Wert. */
    private val _apiKeyEingabe = MutableStateFlow(Einstellungen.apiKey)
    val apiKeyEingabe: StateFlow<String> = _apiKeyEingabe.asStateFlow()

    /** true, solange kein Schluessel hinterlegt ist - dann sind Schreibrouten tot. */
    private val _apiKeyFehlt = MutableStateFlow(!Einstellungen.apiKeyGesetzt)
    val apiKeyFehlt: StateFlow<Boolean> = _apiKeyFehlt.asStateFlow()

    fun apiKeyEingabeAendern(wert: String) {
        _apiKeyEingabe.value = wert
    }

    fun apiKeySpeichern() {
        Einstellungen.apiKey = _apiKeyEingabe.value
        _apiKeyFehlt.value = !Einstellungen.apiKeyGesetzt
        _befehlFeedback.value = if (Einstellungen.apiKeyGesetzt) {
            "API-Schluessel gespeichert. Befehle werden damit angenommen."
        } else {
            "Schluessel entfernt - die App kann nur noch lesen."
        }
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

    fun feedbackQuittieren() {
        _befehlFeedback.value = null
    }

    fun bestaetigungOeffnen() {
        _bestaetigungOffen.value = true
    }

    fun bestaetigungSchliessen() {
        _bestaetigungOffen.value = false
    }

    fun toggleBademodus() {
        viewModelScope.launch {
            val newState = !_isBademodus.value
            repository.setBademodus(newState).fold(
                onSuccess = {
                    _isBademodus.value = newState
                    _befehlFeedback.value =
                        if (newState) "Bademodus aktiviert." else "Bademodus beendet."
                    loadStatus()
                },
                onFailure = { error ->
                    _befehlFeedback.value =
                        "Bademodus konnte nicht umgeschaltet werden: ${error.message ?: "unbekannt"}"
                }
            )
        }
    }

    /**
     * Loest den Not-Aus aus. Die App verliert danach die Verbindung, weil
     * derselbe Prozess auch den Dienst liefert - deshalb wird die Meldung
     * nicht durch einen fehlschlagenden Statusabruf ueberschrieben.
     */
    fun triggerNotAus() {
        viewModelScope.launch {
            repository.triggerNotAus().fold(
                onSuccess = {
                    _dienstBeendet.value = true
                    _befehlFeedback.value =
                        "NOT-AUS ausgefuehrt. Der Dienst hat sich beendet, " +
                        "die App verliert gleich die Verbindung. Wieder annehmen: " +
                        "per SSH 'systemctl restart wpsteuerung', danach hier " +
                        "'Not-Aus aufheben' tippen."
                },
                onFailure = { error ->
                    _befehlFeedback.value =
                        "NOT-AUS FEHLGESCHLAGEN: ${error.message ?: "unbekannt"}"
                }
            )
        }
    }

    /** Hebt eine bestehende Sperre auf - setzt voraus, dass der Dienst laeuft. */
    fun clearNotAus() {
        viewModelScope.launch {
            repository.clearNotAus().fold(
                onSuccess = {
                    _befehlFeedback.value = "Not-Aus aufgehoben, die Steuerung heizt wieder."
                    loadStatus()
                },
                onFailure = { error ->
                    _befehlFeedback.value =
                        "Not-Aus konnte nicht aufgehoben werden: ${error.message ?: "unbekannt"}"
                }
            )
        }
    }

    private fun startAutoRefresh() {
        viewModelScope.launch {
            while (isActive) {
                delay(5000) // Refresh every 5 seconds
                if (_dienstBeendet.value) break
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
