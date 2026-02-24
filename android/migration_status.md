# Vico 3.0 Migration Progress Report

## Current Status
The project is mid-migration from Vico 2.x to Vico 3.0.0-beta.4. 
The app is currently **not compiling** due to API changes in Vico 3.0.

## Changes Made
1. **`android/app/build.gradle.kts`**:
   - Removed `com.patrykandpatrick.vico:core` (merged into other modules in Vico 3).
   - Updated `compileSdk` to **36** (required by Vico 3.0-beta.4).
2. **`HistoryScreen.kt`**:
   - Updated imports from `com.patrykandpatrick.vico.compose.chart.*` to `com.patrykandpatrick.vico.compose.cartesian.*`.
   - Replaced `rememberLineLayer` with `rememberLineCartesianLayer`.
   - Replaced `CartesianChart` with `CartesianChartHost`.
   - Updated Axis API to use `VerticalAxis.rememberStart` and `HorizontalAxis.rememberBottom`.

## Known Issues / Next Steps
- **`fill` function**: The `fill()` helper for colors is unresolved. Research suggests it might be in `com.patrykandpatrick.vico.compose.common.fill`.
- **`CartesianChartModelProducer`**: The static `build()` method is unresolved. It likely needs a constructor or a different factory.
- **`runTransaction`**: This is a suspend function in Vico 3 and needs a coroutine scope.
- **Dependency Type Mismatches**: `lineSeries` block needs clarification for the new Vico 3 layer builder scope.

## How to Resume
1. Open `android/app/src/main/java/com/wpsteuerung/app/ui/screens/HistoryScreen.kt`.
2. Fix the unresolved `fill` and `CartesianChartModelProducer` references.
3. Align the `TemperatureChart` data plotting with the new `CartesianChartModelProducer` transaction API.
