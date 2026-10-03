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
# From Home
for f in "$HOME"/heizungsdaten*.csv*; do
    [ -f "$f" ] && mv "$f" "$TARGET_CSV/" 2>/dev/null
done
# From Project Root (old location)
for f in "$PROJECT_ROOT"/heizungsdaten*.csv*; do
    [ -f "$f" ] && mv "$f" "$TARGET_CSV/" 2>/dev/null
done

# 3. Move Logs
echo "Moving log files..."
# From Home
mv "$HOME"/heizungssteuerung.log.* "$TARGET_STEUERUNG/" 2>/dev/null
# From Project Root
mv "$PROJECT_ROOT"/heizungssteuerung.log* "$TARGET_STEUERUNG/" 2>/dev/null
mv "$PROJECT_ROOT"/error.log* "$TARGET_STEUERUNG/" 2>/dev/null
mv "$PROJECT_ROOT"/*.log "$TARGET_STEUERUNG/" 2>/dev/null
[ -f "$PROJECT_ROOT/kompressor_log.txt" ] && mv "$PROJECT_ROOT/kompressor_log.txt" "$TARGET_STEUERUNG/"

# 4. Move Config and Service files
echo "Moving config and service files..."
[ -f "$PROJECT_ROOT/config.ini" ] && mv "$PROJECT_ROOT/config.ini" "$TARGET_STEUERUNG/"
[ -f "$PROJECT_ROOT/richtige_config.ini" ] && mv "$PROJECT_ROOT/richtige_config.ini" "$TARGET_STEUERUNG/"
[ -f "$PROJECT_ROOT/wpsteuerung.service" ] && mv "$PROJECT_ROOT/wpsteuerung.service" "$TARGET_STEUERUNG/"

# 5. Remove Junk / Save-Files
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
    echo "Consolidating RPI_updater content from home..."
    cp -r "$HOME/RPI_updater"/* "$TARGET_UPDATER/" 2>/dev/null
    rm -rf "$HOME/RPI_updater"
fi

if [ -d "$TARGET_UPDATER/RPI_updater" ]; then
    echo "Removing redundant nested RPI_updater folder..."
    rm -rf "$TARGET_UPDATER/RPI_updater"
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
