package com.wpsteuerung.app.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.wpsteuerung.app.viewmodel.DashboardUiState
import com.wpsteuerung.app.viewmodel.DashboardViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DashboardScreen(
    modifier: Modifier = Modifier,
    viewModel: DashboardViewModel = viewModel()
) {
    val uiState by viewModel.uiState.collectAsState()
    val isBademodus by viewModel.isBademodus.collectAsState()
    val isRefreshing by viewModel.isRefreshing.collectAsState()
    
    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp)
    ) {
        Text(
            text = "Wärmepumpen-Steuerung",
            style = MaterialTheme.typography.headlineMedium,
            fontWeight = FontWeight.Bold
        )
        
        Spacer(modifier = Modifier.height(16.dp))
        
        when (uiState) {
            is DashboardUiState.Loading -> {
                Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
            }
            is DashboardUiState.Success -> {
                val status = (uiState as DashboardUiState.Success).status
                
                // Temperaturen Card
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.primaryContainer
                    )
                ) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Text(
                            text = "Temperaturen",
                            style = MaterialTheme.typography.titleLarge,
                            fontWeight = FontWeight.Bold
                        )
                        Spacer(modifier = Modifier.height(12.dp))
                        
                        TemperatureRow("Oben", status.temperatures.oben)
                        TemperatureRow("Mittig", status.temperatures.mittig)
                        TemperatureRow("Unten", status.temperatures.unten)
                        TemperatureRow("Verdampfer", status.temperatures.verdampfer)
                        status.temperatures.boiler?.let {
                            TemperatureRow("Boiler Ø", it)
                        }
                    }
                }
                
                Spacer(modifier = Modifier.height(16.dp))
                
                // Kompressor Status Card
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(
                        containerColor = if (status.compressor.status == "EIN") 
                            MaterialTheme.colorScheme.errorContainer 
                        else 
                            MaterialTheme.colorScheme.surfaceVariant
                    )
                ) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Text(
                            text = "Kompressor",
                            style = MaterialTheme.typography.titleLarge,
                            fontWeight = FontWeight.Bold
                        )
                        Spacer(modifier = Modifier.height(8.dp))
                        Text(
                            text = status.compressor.status,
                            style = MaterialTheme.typography.headlineSmall,
                            fontWeight = FontWeight.Bold
                        )
                        
                        Spacer(modifier = Modifier.height(8.dp))
                        Text("Heute: ${status.compressor.runtimeToday}")
                        if (status.compressor.status == "EIN") {
                            Text("Aktuelle Laufzeit: ${status.compressor.runtimeCurrent}")
                        }
                    }
                }
                
                Spacer(modifier = Modifier.height(16.dp))
                
                // Modus & Sollwerte Card
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Text(
                            text = "Modus & Sollwerte",
                            style = MaterialTheme.typography.titleLarge,
                            fontWeight = FontWeight.Bold
                        )
                        Spacer(modifier = Modifier.height(8.dp))
                        
                        Text(
                            text = status.mode.current,
                            style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.SemiBold
                        )
                        
                        Spacer(modifier = Modifier.height(8.dp))
                        
                        status.setpoints.einschaltpunkt?.let {
                            Text("Einschaltpunkt: ${String.format("%.1f°C", it)}")
                        }
                        status.setpoints.ausschaltpunkt?.let {
                            Text("Ausschaltpunkt: ${String.format("%.1f°C", it)}")
                        }
                        
                        Spacer(modifier = Modifier.height(8.dp))
                        
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            StatusChip("Solar", status.mode.solarActive)
                            StatusChip("Urlaub", status.mode.holidayActive)
                            StatusChip("Baden", status.mode.bathActive)
                        }
                    }
                }
                
                Spacer(modifier = Modifier.height(16.dp))
                
                // Energie Card
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Text(
                            text = "Energie",
                            style = MaterialTheme.typography.titleLarge,
                            fontWeight = FontWeight.Bold
                        )
                        Spacer(modifier = Modifier.height(8.dp))
                        Text("Batterie: ${status.energy.batteryPower}W")
                        Text("SOC: ${status.energy.soc}%")
                        Text("Einspeisung: ${status.energy.feedIn}W")
                    }
                }
                
                Spacer(modifier = Modifier.height(16.dp))
                
                // Steuerung
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Text(
                            text = "Steuerung",
                            style = MaterialTheme.typography.titleLarge,
                            fontWeight = FontWeight.Bold
                        )
                        Spacer(modifier = Modifier.height(12.dp))
                        
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text("Bademodus")
                            Switch(
                                checked = status.mode.bathActive,
                                onCheckedChange = { viewModel.toggleBademodus() }
                            )
                        }
                    }
                }
                
                // System Info (if exclusion reason exists)
                status.system.exclusionReason?.let { reason ->
                    Spacer(modifier = Modifier.height(16.dp))
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        colors = CardDefaults.cardColors(
                            containerColor = MaterialTheme.colorScheme.tertiaryContainer
                        )
                    ) {
                        Column(modifier = Modifier.padding(16.dp)) {
                            Text(
                                text = "System-Info",
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.Bold
                            )
                            Text(text = reason, style = MaterialTheme.typography.bodyMedium)
                        }
                    }
                }
            }
            is DashboardUiState.Error -> {
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.errorContainer
                    )
                ) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Text("Fehler", style = MaterialTheme.typography.titleLarge)
                        Text((uiState as DashboardUiState.Error).message)
                        Spacer(modifier = Modifier.height(8.dp))
                        Button(onClick = { viewModel.loadStatus() }) {
                            Text("Erneut versuchen")
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun TemperatureRow(label: String, temp: Double?) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text(text = label, fontWeight = FontWeight.Medium)
        Text(
            text = if (temp != null) String.format("%.1f°C", temp) else "N/A",
            fontWeight = FontWeight.Bold
        )
    }
}

@Composable
fun StatusChip(label: String, active: Boolean) {
    Surface(
        color = if (active) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surfaceVariant,
        shape = MaterialTheme.shapes.small
    ) {
        Text(
            text = label,
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp),
            style = MaterialTheme.typography.labelMedium,
            color = if (active) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}
