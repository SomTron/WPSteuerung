package com.wpsteuerung.app.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.patrykandpatrick.vico.compose.cartesian.CartesianChartHost
import com.patrykandpatrick.vico.compose.cartesian.axis.rememberBottomAxis
import com.patrykandpatrick.vico.compose.cartesian.axis.rememberStartAxis
import com.patrykandpatrick.vico.compose.cartesian.data.rememberCartesianChartModelProducer
import com.patrykandpatrick.vico.compose.cartesian.layer.rememberLine
import com.patrykandpatrick.vico.compose.cartesian.layer.rememberLineCartesianLayer
import com.patrykandpatrick.vico.compose.cartesian.rememberCartesianChart
import com.patrykandpatrick.vico.compose.common.component.rememberLineComponent
import com.patrykandpatrick.vico.compose.common.component.rememberTextComponent
import com.patrykandpatrick.vico.compose.common.fill
import com.patrykandpatrick.vico.core.cartesian.data.lineSeries
import com.patrykandpatrick.vico.core.cartesian.layer.LineCartesianLayer
import com.wpsteuerung.app.data.model.HistoryDataPoint
import com.wpsteuerung.app.viewmodel.HistoryUiState
import com.wpsteuerung.app.viewmodel.HistoryViewModel
import java.text.SimpleDateFormat
import java.util.Locale

@Composable
fun HistoryScreen(
    modifier: Modifier = Modifier,
    viewModel: HistoryViewModel = viewModel()
) {
    val uiState by viewModel.uiState.collectAsState()

    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(16.dp)
    ) {
        Text(
            text = "Temperaturverlauf",
            style = MaterialTheme.typography.headlineMedium,
            fontWeight = FontWeight.Bold
        )

        Spacer(modifier = Modifier.height(16.dp))

        when (uiState) {
            is HistoryUiState.Loading -> {
                Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
            }
            is HistoryUiState.Success -> {
                val history = (uiState as HistoryUiState.Success).history

                if (history.data.isNotEmpty()) {
                    // Chart
                    TemperatureChart(
                        data = history.data,
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(300.dp)
                    )

                    Spacer(modifier = Modifier.height(16.dp))

                    // Latest data card
                    Card(modifier = Modifier.fillMaxWidth()) {
                        Column(modifier = Modifier.padding(16.dp)) {
                            Text(
                                text = "Aktuelle Werte (${history.count} Datenpunkte)",
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.Bold
                            )
                            Spacer(modifier = Modifier.height(12.dp))

                            val latest = history.data.last()

                            // Time
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween
                            ) {
                                Text("Zeit:", fontWeight = FontWeight.Medium)
                                Text(formatTimestamp(latest.timestamp))
                            }

                            Spacer(modifier = Modifier.height(8.dp))

                            // Temperatures
                            latest.tOben?.let {
                                TemperatureRow("Oben:", it, Color(0xFFE91E63))
                            }
                            latest.tMittig?.let {
                                TemperatureRow("Mittig:", it, Color(0xFF2196F3))
                            }
                            latest.tUnten?.let {
                                TemperatureRow("Unten:", it, Color(0xFF4CAF50))
                            }
                            latest.tVerd?.let {
                                TemperatureRow("Verdampfer:", it, Color(0xFFFF9800))
                            }

                            Spacer(modifier = Modifier.height(8.dp))

                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween
                            ) {
                                Text("Kompressor:", fontWeight = FontWeight.Medium)
                                Text(
                                    latest.kompressor,
                                    color = if (latest.kompressor == "running")
                                        MaterialTheme.colorScheme.primary
                                    else
                                        MaterialTheme.colorScheme.onSurfaceVariant
                                )
                            }
                        }
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    // Legend
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        colors = CardDefaults.cardColors(
                            containerColor = MaterialTheme.colorScheme.surfaceVariant
                        )
                    ) {
                        Column(modifier = Modifier.padding(12.dp)) {
                            Text(
                                "Legende",
                                style = MaterialTheme.typography.labelLarge,
                                fontWeight = FontWeight.Bold
                            )
                            Spacer(modifier = Modifier.height(8.dp))
                            Row(modifier = Modifier.fillMaxWidth()) {
                                LegendItem("Oben", Color(0xFFE91E63), Modifier.weight(1f))
                                LegendItem("Mittig", Color(0xFF2196F3), Modifier.weight(1f))
                            }
                            Spacer(modifier = Modifier.height(4.dp))
                            Row(modifier = Modifier.fillMaxWidth()) {
                                LegendItem("Unten", Color(0xFF4CAF50), Modifier.weight(1f))
                                LegendItem("Verdampfer", Color(0xFFFF9800), Modifier.weight(1f))
                            }
                        }
                    }
                } else {
                    Card(modifier = Modifier.fillMaxWidth()) {
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(32.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text("Keine Daten verfügbar")
                        }
                    }
                }
            }
            is HistoryUiState.Error -> {
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.errorContainer
                    )
                ) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Text(
                            "Fehler",
                            style = MaterialTheme.typography.titleLarge,
                            color = MaterialTheme.colorScheme.onErrorContainer
                        )
                        Text(
                            (uiState as HistoryUiState.Error).message,
                            color = MaterialTheme.colorScheme.onErrorContainer
                        )
                        Spacer(modifier = Modifier.height(8.dp))
                        Button(onClick = { viewModel.loadHistory() }) {
                            Text("Erneut versuchen")
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun TemperatureChart(
    data: List<HistoryDataPoint>,
    modifier: Modifier = Modifier
) {
    val modelProducer = rememberCartesianChartModelProducer()

    LaunchedEffect(data) {
        // Decimate data for better performance on mobile (max ~300 points)
        val step = if (data.size > 300) data.size / 300 else 1
        val decimatedData = data.filterIndexed { index, _ -> index % step == 0 || index == data.lastIndex }
        
        // Extract temperatures, filtering out nulls
        val obenData = decimatedData.mapNotNull { it.tOben }
        val mittigData = decimatedData.mapNotNull { it.tMittig }
        val untenData = decimatedData.mapNotNull { it.tUnten }
        val verdData = decimatedData.mapNotNull { it.tVerd }
        
        // Compressor background
        val compData = decimatedData.map { if (it.kompressor == "1" || it.kompressor == "running" || it.kompressor == "EIN") 65.0 else 0.0 }

        modelProducer.runTransaction {
            // Create line series
            lineSeries {
                if (compData.any { it > 0 }) series(compData)
                if (obenData.isNotEmpty()) series(obenData)
                if (mittigData.isNotEmpty()) series(mittigData)
                if (untenData.isNotEmpty()) series(untenData)
                if (verdData.isNotEmpty()) series(verdData)
            }
        }
    }

    Card(
        modifier = modifier,
        elevation = CardDefaults.cardElevation(defaultElevation = 4.dp)
    ) {
        CartesianChartHost(
            chart = rememberCartesianChart(
                rememberLineCartesianLayer(
                    lineProvider = LineCartesianLayer.LineProvider.series(
                        LineCartesianLayer.rememberLine(
                            fill = LineCartesianLayer.LineFill.single(fill(Color.Transparent)),
                            areaFill = LineCartesianLayer.AreaFill.single(fill(Color(0x19FF3C3C)))
                        ), // Red background for Compressor
                        LineCartesianLayer.rememberLine(fill = LineCartesianLayer.LineFill.single(fill(Color(0xFFE91E63)))), // Pink for Oben
                        LineCartesianLayer.rememberLine(fill = LineCartesianLayer.LineFill.single(fill(Color(0xFF2196F3)))), // Blue for Mittig
                        LineCartesianLayer.rememberLine(fill = LineCartesianLayer.LineFill.single(fill(Color(0xFF4CAF50)))), // Green for Unten
                        LineCartesianLayer.rememberLine(fill = LineCartesianLayer.LineFill.single(fill(Color(0xFFFF9800))))  // Orange for Verdampfer
                    )
                ),
                startAxis = rememberStartAxis(
                    label = rememberTextComponent(),
                    guideline = rememberLineComponent(
                        fill = fill(Color.LightGray),
                        thickness = 1.dp
                    )
                ),
                bottomAxis = rememberBottomAxis(
                    label = rememberTextComponent()
                )
            ),
            modelProducer = modelProducer,
            modifier = Modifier
                .fillMaxSize()
                .padding(16.dp)
        )
    }
}

@Composable
fun TemperatureRow(label: String, value: Double, color: Color) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier
                    .size(12.dp)
                    .padding(end = 4.dp)
            ) {
                Surface(
                    modifier = Modifier.size(12.dp),
                    color = color,
                    shape = MaterialTheme.shapes.small
                ) {}
            }
            Spacer(modifier = Modifier.width(8.dp))
            Text(label, fontWeight = FontWeight.Medium)
        }
        Text(String.format("%,.1f°C", value))
    }
    Spacer(modifier = Modifier.height(4.dp))
}

@Composable
fun LegendItem(label: String, color: Color, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Surface(
            modifier = Modifier.size(16.dp),
            color = color,
            shape = MaterialTheme.shapes.small
        ) {}
        Spacer(modifier = Modifier.width(8.dp))
        Text(
            label,
            style = MaterialTheme.typography.bodySmall
        )
    }
}

fun formatTimestamp(timestamp: String): String {
    return try {
        val inputFormat = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.getDefault())
        val outputFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())
        val date = inputFormat.parse(timestamp)
        if (date != null) {
            outputFormat.format(date)
        } else {
            timestamp
        }
    } catch (e: Exception) {
        timestamp
    }
}
