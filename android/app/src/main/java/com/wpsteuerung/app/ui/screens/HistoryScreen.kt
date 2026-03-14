package com.wpsteuerung.app.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.patrykandpatrick.vico.compose.axis.horizontal.rememberBottomAxis
import com.patrykandpatrick.vico.compose.axis.vertical.rememberStartAxis
import com.patrykandpatrick.vico.compose.chart.Chart
import com.patrykandpatrick.vico.compose.chart.line.lineChart
import com.patrykandpatrick.vico.compose.chart.line.lineSpec
import com.patrykandpatrick.vico.compose.component.shape.shader.verticalGradient
import com.patrykandpatrick.vico.compose.chart.scroll.rememberChartScrollSpec
import com.patrykandpatrick.vico.core.axis.AxisItemPlacer
import com.patrykandpatrick.vico.core.axis.AxisPosition
import com.patrykandpatrick.vico.core.axis.formatter.AxisValueFormatter
import com.patrykandpatrick.vico.core.entry.ChartEntryModelProducer
import com.patrykandpatrick.vico.core.entry.entryOf
import com.patrykandpatrick.vico.core.scroll.InitialScroll
import com.wpsteuerung.app.data.model.HistoryDataPoint
import com.wpsteuerung.app.ui.theme.ChartColors           // ausgelagert
import com.wpsteuerung.app.util.DateUtils                  // ausgelagert
import com.wpsteuerung.app.util.isKompressorRunning        // ausgelagert
import com.wpsteuerung.app.viewmodel.HistoryUiState
import com.wpsteuerung.app.viewmodel.HistoryViewModel
import androidx.compose.ui.graphics.Color

private const val KOMPRESSOR_BAR_HEIGHT = 65f

@Composable
fun HistoryScreen(
    modifier: Modifier = Modifier,
    viewModel: HistoryViewModel = viewModel()
) {
    val uiState by viewModel.uiState.collectAsState()
    val selectedHours by viewModel.selectedHours.collectAsState()

    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(16.dp)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                text = "Verlauf (letzte ${selectedHours}h)",
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold
            )
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                listOf(1, 6, 12, 24).forEach { hours ->
                    if (hours == selectedHours) {
                        Button(
                            onClick = { viewModel.loadHistory(hours) },
                            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp)
                        ) { Text("${hours}h") }
                    } else {
                        OutlinedButton(
                            onClick = { viewModel.loadHistory(hours) },
                            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp)
                        ) { Text("${hours}h") }
                    }
                }
            }
        }

        Spacer(modifier = Modifier.height(16.dp))

        when (val state = uiState) {
            is HistoryUiState.Loading -> {
                Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
            }

            is HistoryUiState.Success -> {
                val history = state.history

                if (history.data.isNotEmpty()) {
                    TemperatureChart(
                        data = history.data,
                        modifier = Modifier.fillMaxWidth().height(300.dp)
                    )

                    Spacer(modifier = Modifier.height(16.dp))

                    val latest = history.data.last()

                    Card(modifier = Modifier.fillMaxWidth()) {
                        Column(modifier = Modifier.padding(16.dp)) {
                            Text(
                                text = "Aktuelle Werte (${history.count} Datenpunkte)",
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.Bold
                            )
                            Spacer(modifier = Modifier.height(12.dp))
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween
                            ) {
                                Text("Zeit:", fontWeight = FontWeight.Medium)
                                Text(DateUtils.formatTimestamp(latest.timestamp))
                            }
                            Spacer(modifier = Modifier.height(8.dp))
                            latest.tOben?.let    { HistoryTemperatureRow("Oben:",       it, ChartColors.Oben) }
                            latest.tMittig?.let  { HistoryTemperatureRow("Mittig:",     it, ChartColors.Mittig) }
                            latest.tUnten?.let   { HistoryTemperatureRow("Unten:",      it, ChartColors.Unten) }
                            latest.tVorlauf?.let { HistoryTemperatureRow("Vorlauf:",    it, ChartColors.Vorlauf) }
                            latest.tVerd?.let    { HistoryTemperatureRow("Verdampfer:", it, ChartColors.Verdampfer) }
                            latest.tBoiler?.let  { HistoryTemperatureRow("Boiler:",     it, ChartColors.Boiler) }
                            Spacer(modifier = Modifier.height(8.dp))
                            latest.setpointOn?.let  { HistoryTemperatureRow("Soll-Ein:", it, ChartColors.SetpointOn) }
                            latest.setpointOff?.let { HistoryTemperatureRow("Soll-Aus:", it, ChartColors.SetpointOff) }
                            Spacer(modifier = Modifier.height(8.dp))
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween
                            ) {
                                Text("Kompressor:", fontWeight = FontWeight.Medium)
                                Text(
                                    if (latest.kompressor.isKompressorRunning()) "Läuft" else "Aus",
                                    color = if (latest.kompressor.isKompressorRunning())
                                        MaterialTheme.colorScheme.primary
                                    else
                                        MaterialTheme.colorScheme.onSurfaceVariant
                                )
                            }
                        }
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    // Legende
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        colors = CardDefaults.cardColors(
                            containerColor = MaterialTheme.colorScheme.surfaceVariant
                        )
                    ) {
                        Column(modifier = Modifier.padding(12.dp)) {
                            Text("Legende", style = MaterialTheme.typography.labelLarge,
                                fontWeight = FontWeight.Bold)
                            Spacer(modifier = Modifier.height(8.dp))
                            Row(modifier = Modifier.fillMaxWidth()) {
                                LegendItem("Oben",       ChartColors.Oben,       Modifier.weight(1f))
                                LegendItem("Mittig",     ChartColors.Mittig,     Modifier.weight(1f))
                                LegendItem("Unten",      ChartColors.Unten,      Modifier.weight(1f))
                            }
                            Spacer(modifier = Modifier.height(4.dp))
                            Row(modifier = Modifier.fillMaxWidth()) {
                                LegendItem("Vorlauf",    ChartColors.Vorlauf,    Modifier.weight(1f))
                                LegendItem("Verdampfer", ChartColors.Verdampfer, Modifier.weight(1f))
                                LegendItem("Boiler",     ChartColors.Boiler,     Modifier.weight(1f))
                            }
                            Spacer(modifier = Modifier.height(4.dp))
                            Row(modifier = Modifier.fillMaxWidth()) {
                                LegendItem("Soll-Ein",   ChartColors.SetpointOn,  Modifier.weight(1f))
                                LegendItem("Soll-Aus",   ChartColors.SetpointOff, Modifier.weight(1f))
                                Spacer(modifier = Modifier.weight(1f))
                            }
                        }
                    }
                } else {
                    Card(modifier = Modifier.fillMaxWidth()) {
                        Box(
                            modifier = Modifier.fillMaxWidth().padding(32.dp),
                            contentAlignment = Alignment.Center
                        ) { Text("Keine Daten verfügbar") }
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
                        Text("Fehler", style = MaterialTheme.typography.titleLarge,
                            color = MaterialTheme.colorScheme.onErrorContainer)
                        Text(state.message, color = MaterialTheme.colorScheme.onErrorContainer)
                        Spacer(modifier = Modifier.height(8.dp))
                        Button(onClick = { viewModel.retry() }) { Text("Erneut versuchen") }
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
    val sortedData = remember(data) { data.sortedBy { it.timestamp } }

    val startMinutes = remember(sortedData) {
        if (sortedData.isEmpty()) 0L
        else DateUtils.parseToMinutes(sortedData.first().timestamp)
    }

    val modelProducer = remember { ChartEntryModelProducer() }

    val xAxisValueFormatter = remember(startMinutes) {
        AxisValueFormatter<AxisPosition.Horizontal.Bottom> { value, _ ->
            DateUtils.formatMinutesOffset(startMinutes, value.toLong())
        }
    }

    LaunchedEffect(sortedData) {
        fun toX(ts: String) = (DateUtils.parseToMinutes(ts) - startMinutes).toFloat()

        val compEntries   = sortedData.map { d ->
            entryOf(toX(d.timestamp), if (d.kompressor.isKompressorRunning()) KOMPRESSOR_BAR_HEIGHT else 0f)
        }
        val obenEntries   = sortedData.mapNotNull { d -> d.tOben?.let    { entryOf(toX(d.timestamp), it.toFloat()) } }
        val mittigEntries = sortedData.mapNotNull { d -> d.tMittig?.let  { entryOf(toX(d.timestamp), it.toFloat()) } }
        val untenEntries  = sortedData.mapNotNull { d -> d.tUnten?.let   { entryOf(toX(d.timestamp), it.toFloat()) } }
        val vorlaufEntries= sortedData.mapNotNull { d -> d.tVorlauf?.let { entryOf(toX(d.timestamp), it.toFloat()) } }  // NEU
        val verdEntries   = sortedData.mapNotNull { d -> d.tVerd?.let    { entryOf(toX(d.timestamp), it.toFloat()) } }
        val boilerEntries = sortedData.mapNotNull { d -> d.tBoiler?.let  { entryOf(toX(d.timestamp), it.toFloat()) } }  // NEU
        val setOnEntries  = sortedData.mapNotNull { d -> d.setpointOn?.let  { entryOf(toX(d.timestamp), it.toFloat()) } }
        val setOffEntries = sortedData.mapNotNull { d -> d.setpointOff?.let { entryOf(toX(d.timestamp), it.toFloat()) } }

        modelProducer.setEntries(
            compEntries, obenEntries, mittigEntries, untenEntries,
            vorlaufEntries, verdEntries, boilerEntries, setOnEntries, setOffEntries
        )
    }

    Card(modifier = modifier, elevation = CardDefaults.cardElevation(defaultElevation = 4.dp)) {
        Chart(
            chart = lineChart(
                lines = listOf(
                    lineSpec(
                        lineColor = Color.Transparent,
                        lineBackgroundShader = verticalGradient(
                            arrayOf(ChartColors.Kompressor, ChartColors.Kompressor)
                        )
                    ),
                    lineSpec(lineColor = ChartColors.Oben),
                    lineSpec(lineColor = ChartColors.Mittig),
                    lineSpec(lineColor = ChartColors.Unten),
                    lineSpec(lineColor = ChartColors.Vorlauf),    // NEU
                    lineSpec(lineColor = ChartColors.Verdampfer),
                    lineSpec(lineColor = ChartColors.Boiler),     // NEU
                    lineSpec(lineColor = ChartColors.SetpointOn),
                    lineSpec(lineColor = ChartColors.SetpointOff)
                )
            ),
            chartModelProducer = modelProducer,
            chartScrollSpec = rememberChartScrollSpec(initialScroll = InitialScroll.End),
            startAxis = rememberStartAxis(),
            bottomAxis = rememberBottomAxis(
                valueFormatter = xAxisValueFormatter,
                labelRotationDegrees = 45f,
                itemPlacer = remember {
                    AxisItemPlacer.Horizontal.default(spacing = 1, addExtremeLabelPadding = true)
                }
            ),
            modifier = Modifier.fillMaxSize().padding(16.dp)
        )
    }
}

@Composable
fun HistoryTemperatureRow(label: String, value: Double, color: Color) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Surface(modifier = Modifier.size(12.dp), color = color,
                shape = MaterialTheme.shapes.small) {}
            Spacer(modifier = Modifier.width(8.dp))
            Text(label, fontWeight = FontWeight.Medium)
        }
        Text(String.format("%,.1f°C", value))
    }
    Spacer(modifier = Modifier.height(4.dp))
}

@Composable
fun LegendItem(label: String, color: Color, modifier: Modifier = Modifier) {
    Row(modifier = modifier, verticalAlignment = Alignment.CenterVertically) {
        Surface(modifier = Modifier.size(16.dp), color = color,
            shape = MaterialTheme.shapes.small) {}
        Spacer(modifier = Modifier.width(8.dp))
        Text(label, style = MaterialTheme.typography.bodySmall, maxLines = 1)
    }
}