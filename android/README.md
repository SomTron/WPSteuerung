# WP Steuerung Android App

Eine moderne Android-App zur Steuerung und Überwachung der Wärmepumpe mit Jetpack Compose.

## Features

- 📊 **Dashboard**: Live-Anzeige aller Temperaturen, Kompressor-Status und Laufzeiten
- 📈 **Verlauf**: Temperaturverlauf der letzten Stunden
- 🎛️ **Steuerung**: Bademodus On/Off
- 🔄 **Auto-Refresh**: Automatische Aktualisierung alle 5 Sekunden
- 🌙 **Material 3 Design**: Modernes UI mit Light/Dark Theme

## Projekt-Struktur

```
android/
├── app/
│   ├── build.gradle.kts
│   └── src/main/
│       ├── AndroidManifest.xml
│       ├── java/com/wpsteuerung/app/
│       │   ├── MainActivity.kt
│       │   ├── data/
│       │   │   ├── model/          # Data classes
│       │   │   ├── api/            # Retrofit API
│       │   │   └── repository/     # Repository pattern
│       │   ├── viewmodel/          # ViewModels
│       │   └── ui/
│       │       ├── screens/        # Compose screens
│       │       └── theme/          # Material Theme
│       └── res/
├── build.gradle.kts
└── settings.gradle.kts
```

## Installation

### Voraussetzungen
- Android Studio Hedgehog (2023.1.1) oder neuer
- JDK 17
- Raspberry Pi mit laufender API (siehe `../README.md`)

### Schritt 1: Projekt öffnen
1. Android Studio öffnen
2. "Open" → Navigate to `WPSteuerung/android`
3. Gradle Sync abwarten

### Schritt 2: API-URL anpassen
In `app/src/main/java/com/wpsteuerung/app/data/api/RetrofitClient.kt`:
```kotlin
private const val BASE_URL = "http://192.168.0.104:5000/"  // Deine Pi-IP eintragen
```

### Schritt 3: App auf Gerät installieren
1. Android-Gerät per USB verbinden
2. USB-Debugging aktivieren
3. In Android Studio: Run → Run 'app'

## API-Integration

Die App verwendet **Retrofit** für HTTP-Requests:

- `GET /status` - Dashboard-Daten (alle 5s)
- `GET /history?hours=6&limit=100` - Temperaturverlauf
- `POST /control` - Steuerung (Bademodus, Urlaubsmodus)

## Architektur

**MVVM Pattern**:
- **Model**: Data classes (`SystemStatus`, `HistoryResponse`)
- **View**: Composables (`DashboardScreen`, `HistoryScreen`)
- **ViewModel**: Business Logic (`DashboardViewModel`, `HistoryViewModel`)

**Jetpack Compose**:
- Deklaratives UI
- Material 3 Components
- Navigation Component

## Troubleshooting

**App verbindet nicht zur API:**
- Prüfe IP-Adresse in `RetrofitClient.kt`
- Handy muss im gleichen WLAN sein wie der Pi
- Teste API im Browser: `http://192.168.0.104:5000/status`

**Build-Fehler:**
- Gradle Sync durchführen
- Build → Clean Project → Rebuild Project
