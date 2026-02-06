#!/bin/bash
# cleanup_pi.sh - Utility to clean up legacy files and consolidate data on Raspberry Pi
# Targets the new 'Steuerung' and 'Updater' directory structure.

PROJECT_ROOT="$HOME/WPSteuerung"
TARGET_STEUERUNG="$PROJECT_ROOT/Steuerung"
TARGET_CSV="$TARGET_STEUERUNG/csv log"
TARGET_UPDATER="$PROJECT_ROOT/Updater"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "Starting Raspberry Pi Cleanup..."

# 1. Ensure Target Directories exist
mkdir -p "$TARGET_CSV" "$TARGET_STEUERUNG" "$TARGET_UPDATER"

# 2. Consolidate CSV Data
echo "Consolidating CSV files..."
if [ -f "$HOME/heizungsdaten.csv" ]; then
    if [ -f "$TARGET_CSV/heizungsdaten.csv" ]; then
        mv "$HOME/heizungsdaten.csv" "$TARGET_CSV/heizungsdaten_legacy_$TIMESTAMP.csv"
    else
        mv "$HOME/heizungsdaten.csv" "$TARGET_CSV/"
    fi
fi

[ -f "$HOME/heizungsdaten.csv.backup" ] && mv "$HOME/heizungsdaten.csv.backup" "$TARGET_CSV/"
[ -f "$HOME/heizungsdaten2.csv" ] && mv "$HOME/heizungsdaten2.csv" "$TARGET_CSV/"

# 3. Move Logs
echo "Moving log files..."
mv "$HOME"/heizungssteuerung.log.* "$TARGET_STEUERUNG/" 2>/dev/null
[ -f "$HOME/kompressor_log.txt" ] && mv "$HOME/kompressor_log.txt" "$TARGET_STEUERUNG/"
[ -f "$HOME/telegram_debug.log" ] && mv "$HOME/telegram_debug.log" "$TARGET_STEUERUNG/"

# 4. Remove Junk / Save-Files
echo "Removing legacy save files and backups..."
rm -f "$HOME"/telegram_handler.py.save*
rm -f "$HOME"/telegram_handler.py*bak
rm -f "$HOME"/WW_skript.py.save*
rm -f "$HOME"/WW_skript*.bak
rm -f "$HOME"/WW_skript_*.bak
rm -f "$HOME"/*.save
rm -f "$HOME"/debug_*.txt
rm -f "$HOME"/heizungsdaten.csv.bak.gz
rm -f "$HOME"/heizungsdaten.csv.backup

# 5. Remove Duplicate Production Code from Home (it's in $TARGET_STEUERUNG now)
echo "Removing duplicate scripts from home directory..."
rm -f "$HOME/api.py" "$HOME/control_logic.py" "$HOME/telegram_handler.py" "$HOME/utils.py"
rm -f "$HOME/test_heizungssteuerung.py" "$HOME/test.py" "$HOME/webserver.py"
rm -f "$HOME/config_loader.py" "$HOME/correct_timestamps.py" "$HOME/csv_server.py"
rm -f "$HOME/2PunktRegelung.py" "$HOME/state.json"

# 6. Cleanup RPI_updater (if everything is in $TARGET_UPDATER)
if [ -d "$HOME/RPI_updater" ]; then
    echo "Consolidating RPI_updater content..."
    cp -r "$HOME/RPI_updater"/* "$TARGET_UPDATER/" 2>/dev/null
    rm -rf "$HOME/RPI_updater"
fi

# 7. Cleanup Nested Project Dir
if [ -d "$PROJECT_ROOT/WPSteuerung" ]; then
    echo "Removing legacy nested WPSteuerung folder..."
    rm -rf "$PROJECT_ROOT/WPSteuerung"
fi

echo "Cleanup complete!"
echo "Please verify: ls -l $HOME"
echo "Project files are now in: $PROJECT_ROOT/Steuerung"
echo "Updater scripts are now in: $PROJECT_ROOT/Updater"
