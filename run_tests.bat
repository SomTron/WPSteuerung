@echo off
echo Running Tests...
REM Um den Telegram-Test auszuführen, müssen TELEGRAM_BOT_TOKEN und TELEGRAM_CHAT_ID als Umgebungsvariablen gesetzt sein.
REM Beispiel (vorher im Terminal ausführen):
REM set TELEGRAM_BOT_TOKEN=dein_token
REM set TELEGRAM_CHAT_ID=deine_chat_id
python -m pytest -s tests/
pause
