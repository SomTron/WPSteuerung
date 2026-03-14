package com.wpsteuerung.app.ui.screens

import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.wpsteuerung.app.viewmodel.DashboardUiState
import com.wpsteuerung.app.viewmodel.DashboardViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DashboardScreen(
    modifier: Modifier = Modifier,
    viewModel: DashboardViewModel = viewModel()
) {
    val uiState      by viewModel.uiState.collectAsState()
    val controlError by viewModel.controlError.collectAsState()
    var showHolidayDialog by remember { mutableStateOf(false) }
    val snackbarHostState = remember { SnackbarHostState() }

    LaunchedEffect(controlError) {
        controlError?.let {
            snackbarHostState.showSnackbar(it)
            viewModel.clearControlError()
        }
    }

    val isRefreshing by viewModel.isRefreshing.collectAsState()

    Scaffold(
        snackbarHost = { SnackbarHost(snackbarHostState) }
    ) { innerPadding ->
        // PullToRefresh: nach unten ziehen lädt manuell neu
        PullToRefreshBox(
            isRefreshing = isRefreshing,
            onRefresh = { viewModel.loadStatus() },
            modifier = Modifier.padding(innerPadding)
        ) {
            Column(
                modifier = modifier
                    .fillMaxSize()
                    .verticalScroll(rememberScrollState())
                    .padding(horizontal = 16.dp, vertical = 12.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                Text(
                    text = "Wärmepumpe",
                    style = MaterialTheme.typography.headlineMedium,
                    fontWeight = FontWeight.Bold
                )

                when (val state = uiState) {
                    is DashboardUiState.Loading -> {
                        Box(
                            modifier = Modifier.fillMaxWidth().height(300.dp),
                            contentAlignment = Alignment.Center
                        ) { CircularProgressIndicator() }
                    }

                    is DashboardUiState.Success -> {
                        val status = state.status

                        // Kompressor
                        CompressorStatusCard(
                            status           = status.compressor.status,
                            runtimeToday     = status.compressor.runtimeToday,
                            runtimeCurrent   = status.compressor.runtimeCurrent,
                            blockingReason   = status.compressor.blockingReason,
                            activationReason = status.compressor.activationReason
                        )

                        // Temperaturen
                        SectionCard(title = "Temperaturen") {
                            val temps = listOf(
                                "Oben"       to status.temperatures.oben,
                                "Mittig"     to status.temperatures.mittig,
                                "Unten"      to status.temperatures.unten,
                                "Vorlauf"    to status.temperatures.vorlauf,
                                "Verdampfer" to status.temperatures.verdampfer,
                                "Boiler Ø"  to status.temperatures.boiler,
                            ).filter { it.second != null }
                            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                                temps.forEach { (label, temp) -> TemperatureBar(label, temp!!) }
                            }
                        }

                        // Modus & Sollwerte
                        SectionCard(title = "Modus & Sollwerte") {
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Text(
                                    text = status.mode.current,
                                    style = MaterialTheme.typography.titleMedium,
                                    fontWeight = FontWeight.SemiBold,
                                    color = MaterialTheme.colorScheme.primary
                                )
                                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                    StatusChip("Solar",  status.mode.solarActive)
                                    StatusChip("Urlaub", status.mode.holidayActive)
                                    StatusChip("Baden",  status.mode.bathActive)
                                }
                            }
                            Spacer(modifier = Modifier.height(8.dp))
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.spacedBy(12.dp)
                            ) {
                                status.setpoints.einschaltpunkt?.let {
                                    SetpointChip("Einschalt", it, MaterialTheme.colorScheme.tertiary, Modifier.weight(1f))
                                }
                                status.setpoints.ausschaltpunkt?.let {
                                    SetpointChip("Ausschalt", it, MaterialTheme.colorScheme.error, Modifier.weight(1f))
                                }
                            }
                            status.setpoints.activeSensor?.let {
                                Spacer(modifier = Modifier.height(4.dp))
                                Text("Sensor: $it", style = MaterialTheme.typography.labelMedium,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }

                        // Energie
                        SectionCard(title = "Energie") {
                            status.energy.soc?.let { soc ->
                                BatteryBar(soc = soc, batteryPower = status.energy.batteryPower)
                                Spacer(modifier = Modifier.height(12.dp))
                            }
                            // Alle Felder immer anzeigen — 0 W wenn Wert nicht verfügbar
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.spacedBy(8.dp)
                            ) {
                                EnergyMetric(
                                    label    = "Batterie",
                                    value    = "${(status.energy.batteryPower ?: 0.0).toInt()} W",
                                    positive = (status.energy.batteryPower ?: 0.0) >= 0,
                                    modifier = Modifier.weight(1f)
                                )
                                EnergyMetric(
                                    label    = "PV",
                                    value    = "${(status.energy.pvPower ?: 0.0).toInt()} W",
                                    positive = true,
                                    modifier = Modifier.weight(1f)
                                )
                                // feed_in > 0 = Einspeisung, feed_in < 0 = Netzbezug
                                val feedIn = status.energy.feedIn ?: 0.0
                                EnergyMetric(
                                    label    = if (feedIn >= 0) "Einspeisung" else "Netzbezug",
                                    value    = "${kotlin.math.abs(feedIn.toInt())} W",
                                    positive = feedIn >= 0,
                                    modifier = Modifier.weight(1f)
                                )
                            }
                        }

                        // Solarprognose
                        status.forecast?.let { forecast ->
                            SectionCard(title = "Solarprognose") {
                                Row(
                                    modifier = Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.spacedBy(12.dp)
                                ) {
                                    forecast.today?.let    { ForecastMetric("Heute",  it, Modifier.weight(1f)) }
                                    forecast.tomorrow?.let { ForecastMetric("Morgen", it, Modifier.weight(1f)) }
                                }
                                if (forecast.sunrise != null && forecast.sunset != null) {
                                    Spacer(modifier = Modifier.height(8.dp))
                                    Row(
                                        modifier = Modifier.fillMaxWidth(),
                                        horizontalArrangement = Arrangement.Center
                                    ) {
                                        Text("🌅 ${forecast.sunrise}")
                                        Spacer(modifier = Modifier.width(24.dp))
                                        Text("🌇 ${forecast.sunset}")
                                    }
                                }
                            }
                        }

                        // Steuerung
                        SectionCard(title = "Steuerung") {
                            ControlRow("Bademodus", status.mode.bathActive) { viewModel.toggleBademodus() }
                            HorizontalDivider(modifier = Modifier.padding(vertical = 8.dp))
                            ControlRow("Urlaubsmodus", status.mode.holidayActive) {
                                if (it) showHolidayDialog = true else viewModel.setUrlaubsmodus(false)
                            }
                        }

                        // System-Info
                        status.system.exclusionReason?.let { reason ->
                            Card(
                                modifier = Modifier.fillMaxWidth(),
                                colors = CardDefaults.cardColors(
                                    containerColor = MaterialTheme.colorScheme.tertiaryContainer
                                )
                            ) {
                                Row(
                                    modifier = Modifier.padding(16.dp),
                                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    Text("ℹ️", fontSize = 20.sp)
                                    Column {
                                        Text("System-Info", style = MaterialTheme.typography.labelLarge,
                                            fontWeight = FontWeight.Bold,
                                            color = MaterialTheme.colorScheme.onTertiaryContainer)
                                        Text(reason, style = MaterialTheme.typography.bodyMedium,
                                            color = MaterialTheme.colorScheme.onTertiaryContainer)
                                    }
                                }
                            }
                        }

                        if (showHolidayDialog) {
                            HolidayDurationDialog(
                                onDismiss = { showHolidayDialog = false },
                                onConfirm = { hours ->
                                    viewModel.setUrlaubsmodus(true, hours)
                                    showHolidayDialog = false
                                }
                            )
                        }
                    }

                    is DashboardUiState.Error -> {
                        Card(
                            modifier = Modifier.fillMaxWidth(),
                            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)
                        ) {
                            Column(modifier = Modifier.padding(16.dp)) {
                                Text("Verbindungsfehler", style = MaterialTheme.typography.titleLarge,
                                    color = MaterialTheme.colorScheme.onErrorContainer, fontWeight = FontWeight.Bold)
                                Spacer(modifier = Modifier.height(4.dp))
                                Text(state.message, color = MaterialTheme.colorScheme.onErrorContainer)
                                Spacer(modifier = Modifier.height(12.dp))
                                Button(onClick = { viewModel.loadStatus() }) { Text("Erneut versuchen") }
                            }
                        }
                    }
                }
            }
        } // PullToRefreshBox
    }
}

@Composable
fun CompressorStatusCard(
    status: String,
    runtimeToday: String,
    runtimeCurrent: String,
    blockingReason: String?,
    activationReason: String?
) {
    val isRunning = status == "EIN"
    val infiniteTransition = rememberInfiniteTransition(label = "pulse")
    val pulseScale by infiniteTransition.animateFloat(
        initialValue = 1f,
        targetValue  = if (isRunning) 1.18f else 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(900, easing = EaseInOut),
            repeatMode = RepeatMode.Reverse
        ),
        label = "pulseScale"
    )

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = if (isRunning) MaterialTheme.colorScheme.primaryContainer
            else MaterialTheme.colorScheme.surfaceVariant
        )
    ) {
        Row(
            modifier = Modifier.padding(16.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            Box(
                modifier = Modifier
                    .size(52.dp)
                    .scale(pulseScale)
                    .clip(CircleShape)
                    .background(
                        if (isRunning) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.outline.copy(alpha = 0.25f)
                    ),
                contentAlignment = Alignment.Center
            ) {
                Text(
                    text = if (isRunning) "▶" else "⏸",
                    fontSize = 20.sp,
                    color = if (isRunning) MaterialTheme.colorScheme.onPrimary
                    else MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Column(modifier = Modifier.weight(1f)) {
                Text("Kompressor", style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text(
                    text = status,
                    style = MaterialTheme.typography.headlineSmall,
                    fontWeight = FontWeight.Bold,
                    color = if (isRunning) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.onSurfaceVariant
                )
                blockingReason?.let {
                    Text(it, style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                activationReason?.let {
                    Text(it, style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.primary)
                }
            }
            Column(horizontalAlignment = Alignment.End) {
                Text("Heute", style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text(runtimeToday, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                if (isRunning) {
                    Spacer(modifier = Modifier.height(4.dp))
                    Text("Aktuell", style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text(runtimeCurrent, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Medium)
                }
            }
        }
    }
}

@Composable
fun TemperatureBar(label: String, temp: Double) {
    val fraction = (temp.coerceIn(0.0, 60.0) / 60.0).toFloat()
    val barColor = when {
        fraction < 0.33f -> lerp(Color(0xFF2196F3), Color(0xFF4CAF50), fraction / 0.33f)
        fraction < 0.66f -> lerp(Color(0xFF4CAF50), Color(0xFFFF9800), (fraction - 0.33f) / 0.33f)
        else             -> lerp(Color(0xFFFF9800), Color(0xFFE91E63), (fraction - 0.66f) / 0.34f)
    }
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        Text(label, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Medium,
            modifier = Modifier.width(80.dp))
        Box(
            modifier = Modifier.weight(1f).height(8.dp).clip(MaterialTheme.shapes.extraSmall)
                .background(MaterialTheme.colorScheme.surfaceVariant)
        ) {
            Box(modifier = Modifier.fillMaxHeight().fillMaxWidth(fraction)
                .clip(MaterialTheme.shapes.extraSmall).background(barColor))
        }
        Text(String.format("%.1f°C", temp), style = MaterialTheme.typography.bodyMedium,
            fontWeight = FontWeight.Bold, color = barColor, modifier = Modifier.width(60.dp))
    }
}

@Composable
fun BatteryBar(soc: Double, batteryPower: Double?) {
    val fraction = (soc.coerceIn(0.0, 100.0) / 100.0).toFloat()
    val barColor = when {
        fraction < 0.2f -> MaterialTheme.colorScheme.error
        fraction < 0.5f -> Color(0xFFFF9800)
        else            -> Color(0xFF4CAF50)
    }
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        Text(if ((batteryPower ?: 0.0) > 0) "🔋⬆" else "🔋", fontSize = 16.sp)
        Box(
            modifier = Modifier.weight(1f).height(12.dp).clip(MaterialTheme.shapes.extraSmall)
                .background(MaterialTheme.colorScheme.surfaceVariant)
        ) {
            Box(modifier = Modifier.fillMaxHeight().fillMaxWidth(fraction)
                .clip(MaterialTheme.shapes.extraSmall).background(barColor))
        }
        Text("${soc.toInt()}%", style = MaterialTheme.typography.bodyMedium,
            fontWeight = FontWeight.Bold, color = barColor, modifier = Modifier.width(44.dp))
    }
}

@Composable
fun SectionCard(title: String, content: @Composable ColumnScope.() -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 1.dp)
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Spacer(modifier = Modifier.height(12.dp))
            content()
        }
    }
}

@Composable
fun SetpointChip(label: String, value: Double, color: Color, modifier: Modifier = Modifier) {
    Surface(modifier = modifier, color = color.copy(alpha = 0.12f), shape = MaterialTheme.shapes.small) {
        Column(modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
            horizontalAlignment = Alignment.CenterHorizontally) {
            Text(label, style = MaterialTheme.typography.labelSmall, color = color)
            Text(String.format("%.1f°C", value), style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold, color = color)
        }
    }
}

@Composable
fun EnergyMetric(label: String, value: String, positive: Boolean, modifier: Modifier = Modifier) {
    val color = if (positive) Color(0xFF4CAF50) else MaterialTheme.colorScheme.error
    Surface(modifier = modifier, color = color.copy(alpha = 0.1f), shape = MaterialTheme.shapes.small) {
        Column(modifier = Modifier.padding(8.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Text(label, style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text(value, style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.Bold, color = color)
        }
    }
}

@Composable
fun ForecastMetric(label: String, value: Double, modifier: Modifier = Modifier) {
    Surface(modifier = modifier, color = MaterialTheme.colorScheme.primaryContainer,
        shape = MaterialTheme.shapes.small) {
        Column(modifier = Modifier.padding(12.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Text(label, style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onPrimaryContainer)
            Text(String.format("%.1f kWh", value), style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.primary)
        }
    }
}

@Composable
fun ControlRow(label: String, checked: Boolean, onCheckedChange: (Boolean) -> Unit) {
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically) {
        Text(label, style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.Medium)
        Switch(checked = checked, onCheckedChange = onCheckedChange)
    }
}

@Composable
fun StatusChip(label: String, active: Boolean) {
    Surface(
        color = if (active) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surfaceVariant,
        shape = MaterialTheme.shapes.extraSmall
    ) {
        Text(
            text = label,
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp),
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.Medium,
            color = if (active) MaterialTheme.colorScheme.onPrimary
            else MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}

@Composable
fun HolidayDurationDialog(onDismiss: () -> Unit, onConfirm: (Int?) -> Unit) {
    var durationText by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Urlaubsmodus aktivieren") },
        text = {
            Column {
                Text("Wie lange soll der Urlaubsmodus aktiv bleiben?")
                Spacer(modifier = Modifier.height(16.dp))
                OutlinedTextField(
                    value = durationText,
                    onValueChange = { if (it.all { c -> c.isDigit() }) durationText = it },
                    label = { Text("Dauer in Stunden (optional)") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.fillMaxWidth()
                )
                Text("Leer lassen für unbegrenzte Dauer.", style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(top = 4.dp))
            }
        },
        confirmButton = { Button(onClick = { onConfirm(durationText.toIntOrNull()) }) { Text("Aktivieren") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Abbrechen") } }
    )
}

// Kompatibilität mit HistoryScreen
@Composable
fun TemperatureRow(label: String, temp: Double?) {
    if (temp == null) return
    Row(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween) {
        Text(label, fontWeight = FontWeight.Medium)
        Text(String.format("%.1f°C", temp), fontWeight = FontWeight.Bold)
    }
}
