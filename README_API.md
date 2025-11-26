# WPSteuerung API - Entwicklungsleitfaden

## Übersicht

Die API ermöglicht die Steuerung der Wärmepumpe über eine Android App. Die Entwicklung erfolgt in mehreren Stufen:

1. **PC-Entwicklung** (aktuell): API läuft auf dem PC mit Mock-Daten
2. **Handy-Tests**: Android App verbindet sich zum PC über das Netzwerk
3. **Raspberry Pi**: Integration mit echter Hardware

## Schnellstart - PC Entwicklung

### 1. API Server starten

```bash
cd C:\Users\Patrik\PycharmProjects\WPSteuerungGit\WPSteuerung
python api_server.py
```

Der Server läuft dann auf: `http://localhost:5000`

### 2. API testen

#### Mit dem Browser:
- Swagger UI (interaktive Dokumentation): http://localhost:5000/docs
- ReDoc: http://localhost:5000/redoc

#### Mit curl/PowerShell:

**Status abrufen:**
```powershell
Invoke-RestMethod -Uri "http://localhost:5000/status" -Method Get | ConvertTo-Json
```

**Kompressor einschalten:**
```powershell
$body = @{
    command = "force_on"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:5000/control" -Method Post -Body $body -ContentType "application/json"
```

**Bademodus aktivieren:**
```powershell
$body = @{
    command = "set_mode"
    params = @{
        mode = "bademodus"
        active = $true
    }
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:5000/control" -Method Post -Body $body -ContentType "application/json"
```

## API Endpoints

### GET /status
Liefert den aktuellen Systemstatus.

**Response:**
```json
{
  "temperatures": {
    "oben": 42.5,
    "mittig": 41.0,
    "unten": 39.5,
    "verdampfer": 10.2,
    "boiler": 41.0
  },
  "compressor": {
    "status": "AUS",
    "runtime_current": "0:00:00",
    "runtime_today": "2:30:00"
  },
  "mode": {
    "current": "Normalmodus",
    "solar_active": false,
    "holiday_active": false,
    "bath_active": false
  },
  "energy": {
    "battery_power": 250,
    "soc": 75,
    "feed_in": 100
  },
  "system": {
    "exclusion_reason": null,
    "last_update": "15:30:45",
    "mode": "development"
  }
}
```

### POST /control
Steuert das System (Kompressor, Modi).

**Request Body:**
```json
{
  "command": "force_on",  // oder "force_off", "set_mode"
  "params": {
    "mode": "bademodus",  // nur für "set_mode"
    "active": true        // nur für "set_mode"
  }
}
```

### POST /config
Aktualisiert Konfigurationswerte (in Dev-Mode nicht persistent).

**Request Body:**
```json
{
  "section": "Heizungssteuerung",
  "key": "EINSCHALTPUNKT",
  "value": "42"
}
```

## Android App Entwicklung

### Verbindung zum PC

1. PC und Handy müssen im selben Netzwerk sein
2. PC-IP-Adresse ermitteln: `ipconfig` in PowerShell
3. In der Android App die IP verwenden: `http://192.168.x.x:5000`

### Beispiel Android Code (Kotlin mit Retrofit):

```kotlin
interface WPSteuerungApi {
    @GET("status")
    suspend fun getStatus(): StatusResponse
    
    @POST("control")
    suspend fun sendControl(@Body command: ControlCommand): ControlResponse
}

// Verwendung:
val retrofit = Retrofit.Builder()
    .baseUrl("http://192.168.1.100:5000/")  // PC-IP eintragen
    .addConverterFactory(GsonConverterFactory.create())
    .build()

val api = retrofit.create(WPSteuerungApi::class.java)
val status = api.getStatus()
```

## Nächste Schritte

Nach erfolgreicher Entwicklung auf dem PC:

1. **Android App fertigstellen**: Alle Funktionen implementieren und testen
2. **Raspberry Pi Integration**: `api.py` mit echtem `state` Object verbinden
3. **Deployment**: API auf dem Raspberry Pi starten (im `main.py`)

## Firewall Hinweis

Falls die Verbindung vom Handy nicht funktioniert, muss eventuell Port 5000 in der Windows Firewall freigegeben werden:

```powershell
# Als Administrator ausführen:
New-NetFirewallRule -DisplayName "WPSteuerung API" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow
```
