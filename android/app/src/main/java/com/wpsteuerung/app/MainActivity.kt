package com.wpsteuerung.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.ShowChart
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import com.wpsteuerung.app.ui.screens.DashboardScreen
import com.wpsteuerung.app.ui.screens.HistoryScreen
import com.wpsteuerung.app.ui.theme.WPSteuerungTheme

// HINWEIS: Für Material Icons Icons.Filled.ShowChart benötigst du:
// implementation("androidx.compose.material:material-icons-extended:<version>")

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge() // FIX: Edge-to-Edge für modernes Android-Look
        setContent {
            WPSteuerungTheme {
                MainApp()
            }
        }
    }
}

// FIX: sealed class + companion object hatte Initialisierungsproblem →
// einfaches enum ist zuverlässiger für Navigation
enum class NavDestination(val label: String, val icon: ImageVector) {
    Dashboard("Dashboard", Icons.Filled.Home),
    History("Verlauf",     Icons.Filled.ShowChart)
}

@Composable
fun MainApp() {
    var selectedDestination by remember { mutableStateOf(NavDestination.Dashboard) }

    Scaffold(
        modifier = Modifier.fillMaxSize(),
        bottomBar = {
            NavigationBar {
                NavDestination.entries.forEach { destination ->
                    NavigationBarItem(
                        selected = selectedDestination == destination,
                        onClick  = { selectedDestination = destination },
                        icon = {
                            Icon(
                                imageVector = destination.icon,
                                contentDescription = destination.label
                            )
                        },
                        label = { Text(destination.label) }
                    )
                }
            }
        }
    ) { padding ->
        when (selectedDestination) {
            NavDestination.Dashboard -> DashboardScreen(modifier = Modifier.padding(padding))
            NavDestination.History   -> HistoryScreen(modifier = Modifier.padding(padding))
        }
    }
}