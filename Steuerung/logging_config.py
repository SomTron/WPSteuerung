import logging
import sys
import os
import asyncio
import aiohttp
from logging.handlers import RotatingFileHandler

class TelegramHandler(logging.Handler):
    def __init__(self, bot_token, chat_id, session=None, level=logging.NOTSET):
        super().__init__(level)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session = session
        self.queue = asyncio.Queue(maxsize=50)
        self.dropped_messages = 0
        self.task = None
        self.loop = None
        self._loop_owner = False

    async def send_message(self, message):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message
        }
        
        # Session Handling: If session is provided use it, otherwise create one-off
        if self.session and not self.session.closed:
             try:
                async with self.session.post(url, json=payload, timeout=20) as response:
                    if response.status == 200:
                        return True
                    else:
                        error_text = await response.text()
                        logging.error(f"Fehler beim Senden an Telegram: {response.status} - {error_text}")
                        return False
             except Exception as e:
                 logging.error(f"Telegram Senden Fehler: {e}")
                 return False
        else:
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(url, json=payload, timeout=20) as response:
                        if response.status == 200:
                            return True
                        # ... error handling ...
                        return False
                except Exception:
                    return False

    def emit(self, record):
        try:
            msg = self.format(record)
            if self.loop is None:
                try:
                    self.loop = asyncio.get_running_loop()
                except RuntimeError:
                    return

            if self.loop.is_closed():
                return

            try:
                self.queue.put_nowait(msg)
            except asyncio.QueueFull:
                # Alte Einträge sind weniger wert als aktuelle Alarme.
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                self.dropped_messages += 1
                try:
                    self.queue.put_nowait(msg)
                except asyncio.QueueFull:
                    self.dropped_messages += 1

            if not self.task or self.task.done():
                self.task = self.loop.create_task(self.process_queue())
        except Exception:
            self.handleError(record)

    async def process_queue(self):
        while not self.queue.empty():
            msg = None
            try:
                msg = await self.queue.get()
                await self.send_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Telegram-Logging-Nachricht konnte nicht gesendet werden")
            finally:
                if msg is not None:
                    self.queue.task_done()
    
    def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
        super().close()

def _resolve_log_dir(explicit=None):
    """Ermittelt das Log-Verzeichnis.

    Default auf dem Raspberry Pi (POSIX mit /var/log): /var/log/wps
    (wird durch log2ram im RAM gehalten und stündlich auf die SD synct).
    Auf anderen Systemen (Entwicklung/Windows): aktuelles Arbeitsverzeichnis.
    Ueberschreibbar per Parameter oder Umgebungsvariable WPS_LOG_DIR.
    """
    if explicit:
        candidate = str(explicit)
    else:
        candidate = os.environ.get("WPS_LOG_DIR") or ""
        if not candidate:
            if os.name == "posix" and os.path.isdir("/var/log"):
                candidate = "/var/log/wps"
            else:
                candidate = os.getcwd()
    try:
        os.makedirs(candidate, exist_ok=True)
        return candidate
    except OSError as e:
        logging.warning(
            f"Log-Verzeichnis {candidate} nicht nutzbar ({e}), "
            "verwende Arbeitsverzeichnis als Fallback."
        )
        candidate = os.getcwd()
        os.makedirs(candidate, exist_ok=True)
        return candidate


def setup_logging(enable_full_log=True, telegram_config=None, session=None, log_dir=None):
    """
    Richtet das Logging ein.
    telegram_config: Objekt mit BOT_TOKEN und CHAT_ID oder None
    log_dir: Log-Verzeichnis (Default: /var/log/wps auf dem Pi, sonst CWD).
    """
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        handler.close()
        root_logger.removeHandler(handler)

    root_logger.setLevel(logging.DEBUG)
    
    # Externe Libs dämpfen
    logging.getLogger('matplotlib').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %z"
    )

    log_dir = _resolve_log_dir(log_dir)

    # Error Log (WARN+): bleibt klein (2x5 MB), damit der log2ram-RAM-Vorrat
    # (/var/log/wps, SIZE=128M) nicht durch das 70-MB-DEBUG-Volumen gesprengt wird.
    error_handler = RotatingFileHandler(
        os.path.join(log_dir, "error.log"), maxBytes=5*1024*1024, backupCount=2, encoding="utf-8"
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)

    # Full Log (DEBUG): auf 2x25 MB begrenzt (vorher 100 MB) - wichtig fuer
    # log2ram, dessen tmpfs-Groesse begrenzt ist und nur stündlich synct.
    if enable_full_log:
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, "heizungssteuerung.log"),
            maxBytes=25*1024*1024, backupCount=2, encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # Console
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    # Telegram
    if telegram_config and telegram_config.BOT_TOKEN and telegram_config.CHAT_ID:
        tg_handler = TelegramHandler(
            telegram_config.BOT_TOKEN, 
            telegram_config.CHAT_ID, 
            session=session,
            level=logging.WARNING
        )
        tg_handler.setFormatter(logging.Formatter("%(message)s"))
        root_logger.addHandler(tg_handler)
        logging.debug("Telegram Logging aktiviert")
    else:
        logging.warning("Telegram Logging nicht konfiguriert")
