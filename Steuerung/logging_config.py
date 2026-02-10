import logging
import sys
import asyncio
import aiohttp
from logging.handlers import RotatingFileHandler

class TelegramHandler(logging.Handler):
    def __init__(self, bot_token, chat_id, session=None, level=logging.NOTSET):
        super().__init__(level)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session = session
        self.queue = asyncio.Queue()
        self.task = None
        self.loop = None
        self._loop_owner = False
        self.last_messages = {} # {msg_content: last_sent_time}

    def _should_send(self, message):
        """Simple deduplication: Only send if message is new or last sent > 1 hour ago."""
        from datetime import datetime, timedelta
        now = datetime.now()
        
        # Normalize message for better matching (e.g. remove timestamps/dynamic values if possible)
        # For now, literal match is safest for logging.
        if message in self.last_messages:
            last_sent = self.last_messages[message]
            if now - last_sent < timedelta(hours=1):
                return False
        
        self.last_messages[message] = now
        # Cleanup old entries to prevent memory leak
        if len(self.last_messages) > 100:
            cutoff = now - timedelta(hours=1)
            self.last_messages = {m: t for m, t in self.last_messages.items() if t > cutoff}
            
        return True

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

            if not self._should_send(msg):
                return

            self.queue.put_nowait(msg)

            if not self.task or self.task.done():
                self.task = self.loop.create_task(self.process_queue())
        except Exception:
            self.handleError(record)

    async def process_queue(self):
        while not self.queue.empty():
            try:
                msg = await self.queue.get()
                await self.send_message(msg)
                self.queue.task_done()
            except Exception:
                pass
    
    def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
        super().close()

def setup_logging(enable_full_log=True, telegram_config=None, session=None):
    """
    Richtet das Logging ein.
    telegram_config: Objekt mit BOT_TOKEN und CHAT_ID oder None
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

    # Error Log
    error_handler = RotatingFileHandler(
        "error.log", maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)

    # Full Log
    if enable_full_log:
        file_handler = RotatingFileHandler(
            "heizungssteuerung.log", maxBytes=100*1024*1024, backupCount=5, encoding="utf-8"
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
