import logging
import sys
import os
import asyncio
import aiohttp
from datetime import date, datetime

from log_files import archiv_name, verwalte_archiv

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

class TagesRotationsHandler(logging.Handler):
    """Rotiert das Log taeglich - Dateiname traegt das Datum.

    Warum nicht der Standard-RotatingFileHandler: der erzeugt `.1`, `.2`,
    `.3` - Zahlen ohne Aussage. Man kann dann nicht sagen, welcher Tag in
    welcher Datei steht, und der Leser muss raten, welche Datei zur
    gesuchten Zeit passt. Hier heisst die Datei `heizungssteuerung.log.
    2026-10-20`: der Tag steht direkt im Namen.

    Zusaetzlich wird bei Ueberschreitung von `max_bytes` innerhalb eines
    Tages rotiert (`...2026-10-20.2`), damit ein auffaelliger Tag nicht
    das ganze RAM-Budget auffrisst.

    `keep_tage` und `max_gesamt_bytes` begrenzen das Archiv: /var/log/wps
    liegt auf dem Pi im log2ram-tmpfs (SIZE=128M), dort darf das Log
    nicht beliebig wachsen.
    """

    def __init__(
        self,
        pfad: str,
        max_bytes: int = 25 * 1024 * 1024,
        keep_tage: int = 14,
        max_gesamt_bytes: int = 60 * 1024 * 1024,
        encoding: str = "utf-8",
    ):
        super().__init__()
        self.pfad = pfad
        self.max_bytes = max_bytes
        self.keep_tage = keep_tage
        self.max_gesamt_bytes = max_gesamt_bytes
        self.encoding = encoding
        self._stream = None
        self._tag = date.today()   # Tag, fuer den die aktive Datei steht
        self._teil = 1             # Teilnummer der aktiven Datei im Tag
        # Beim Start kann die aktive Datei von gestern stammen (Dienst war
        # gestoppt). Dann gehoert sie zu ihrem mtime-Tag, nicht zu heute -
        # sonst wuerde der gestrige Log am Ende als "heute" archiviert.
        if os.path.exists(self.pfad) and self._groesse() > 0:
            try:
                self._tag = datetime.fromtimestamp(
                    os.path.getmtime(self.pfad)
                ).date()
            except OSError:
                self._tag = date.today()
            self._teil = self._naechste_teilnummer(self._tag)

    # -- interne Hilfen ---------------------------------------------------
    def _groesse(self) -> int:
        try:
            return os.path.getsize(self.pfad)
        except OSError:
            return 0

    def _naechste_teilnummer(self, tag: date) -> int:
        """Nummer, die der naechste Archiv-Eintrag fuer `tag` bekommen soll."""
        teil = 1
        while teil < 999:
            if not os.path.exists(archiv_name(self.pfad, tag, teil if teil > 1 else 0)):
                return teil
            teil += 1
        return teil

    def _archiv_verwalten(self) -> None:
        try:
            verwalte_archiv(self.pfad, self.keep_tage, self.max_gesamt_bytes)
        except OSError as e:
            logging.debug(f"Log-Archiv konnte nicht verwaltet werden: {e}")

    # -- logging.Handler --------------------------------------------------
    def emit(self, record: logging.LogRecord) -> None:
        try:
            heute = date.today()
            # Tageswechsel: die aktive Datei gehoert dem gestrigen Tag.
            if heute != self._tag:
                self._rotiere(heute)
            elif self._groesse() >= self.max_bytes:
                # Groessengrenze mitten im Tag gerissen: der Tag wird in
                # mehrere Teile zerlegt, damit der Dateiname eindeutig
                # bleibt.
                self._rotiere(heute, teiler=True)
            if self._stream is None:
                self._stream = open(self.pfad, "a", encoding=self.encoding)
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def _rotiere(self, heute: date, teiler: bool = False) -> None:
        """Schliesst die aktive Datei und legt sie datiert im Archiv ab."""
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        if self._groesse() == 0:
            # Leere Datei: kein Archiv-Eintrag noetig.
            self._tag = heute
            self._teil = 1
            return
        # Die aktive Datei wird unter dem Tag archiviert, zu dem sie gehoert
        # (self._tag), und erhaelt die naechste freie Teilnummer.
        self._teil = self._naechste_teilnummer(self._tag)
        ziel = archiv_name(self.pfad, self._tag, self._teil if self._teil > 1 else 0)
        try:
            os.replace(self.pfad, ziel)
        except OSError as e:
            logging.debug(f"Log-Rotation nicht moeglich: {e}")
        self._archiv_verwalten()
        if not teiler:
            # Tageswechsel: ab jetzt gehoert die aktive Datei zum neuen Tag.
            self._tag = heute
            self._teil = 1

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        except OSError:
            pass
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
    # Auch das Error-Log rotiert tagesbasiert - der Dateiname nennt den Tag,
    # damit ein Fehler vom 15.09. direkt auffindbar bleibt.
    error_handler = TagesRotationsHandler(
        os.path.join(log_dir, "error.log"),
        max_bytes=5 * 1024 * 1024,
        keep_tage=14,
        max_gesamt_bytes=15 * 1024 * 1024,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)

    # Full Log (DEBUG): tagesbasiert rotiert statt groessenbasiert.
    # Warum: die Groessengrenze (25 MB) sagt nichts ueber den Zeitraum aus -
    # an einem Tag mit viel Betrieb kann sie mitten in der Nacht greifen,
    # und der Leser sieht dann nur noch den kleinen Rest. Mit dem Datum im
    # Dateinamen ist der Log eines Tages immer vollstaendig auffindbar.
    # Das Byte-Budget (60 MB) haelt den RAM-Verbrauch im log2ram-tmpfs
    # (SIZE=128M) im Rahmen; die Zeitgrenze (14 Tage) begrenzt die Historie.
    if enable_full_log:
        file_handler = TagesRotationsHandler(
            os.path.join(log_dir, "heizungssteuerung.log"),
            max_bytes=25 * 1024 * 1024,
            keep_tage=14,
            max_gesamt_bytes=60 * 1024 * 1024,
            encoding="utf-8",
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
