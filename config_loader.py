import configparser
import logging

config = configparser.ConfigParser()
if not config.read("config.ini") or "Telegram" not in config:
    logging.critical("config.ini nicht gefunden oder fehlerhaft. Programm wird beendet.")
    exit(1)

try:
    BOT_TOKEN = config["Telegram"]["BOT_TOKEN"]
    CHAT_ID = config["Telegram"]["CHAT_ID"]
except KeyError as e:
    logging.critical(f"Fehler in config.ini: {e} fehlt. Programm wird beendet.")
    exit(1)

if not BOT_TOKEN or not CHAT_ID:
    logging.critical("BOT_TOKEN oder CHAT_ID ist leer. Programm wird beendet.")
    exit(1)