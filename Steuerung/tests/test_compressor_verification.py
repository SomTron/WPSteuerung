import pytest
from unittest.mock import MagicMock
from datetime import datetime, timedelta
import pytz
import sys
import os

# Ensure we can import from parent directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from safety_logic import verify_compressor_running

@pytest.fixture
def mock_state():
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    
    # Sub-states
    state.control = MagicMock()
    state.control.kompressor_ein = True
    
    # Verification fields (at top level for now in State class)
    state.kompressor_verification_error_count = 0
    state.kompressor_verification_failed = False
    state.kompressor_verification_last_check = None
    state.kompressor_verification_start_time = None
    
    # Config/Telegram
    state.config = MagicMock()
    state.config.Telegram.BOT_TOKEN = "mock_token"
    state.config.Telegram.CHAT_ID = "mock_chat_id"
    state.bot_token = state.config.Telegram.BOT_TOKEN
    
    return state

@pytest.mark.asyncio
async def test_verification_delayed_default_10_min(mock_state):
    """
    Test that verification is skipped (returns True) if time < 10 minutes (new default).
    """
    now = datetime(2023, 1, 1, 12, 10, 0, tzinfo=mock_state.local_tz)
    # Start time 9 minutes ago
    start_time = now - timedelta(minutes=9)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_verd = 10.0
    mock_state.kompressor_verification_start_t_unten = 30.0
    
    with pytest.MonkeyPatch.context() as m:
        class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        # Must patch where it's USED: safety_logic imports datetime
        m.setattr("safety_logic.datetime", MockDateTime)
        
        # Should return True because elapsed time (9m) < default delay (10m)
        is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=8.0, current_t_unten=30.0
        )
        
        assert is_running is True
        assert error_msg is None

@pytest.mark.asyncio
async def test_verification_success_cold_start(mock_state):
    """
    Test "Cold Start / Restart" scenario.
    Start T_Verd is low (< 15), Current T_Verd is low (< 12), and did not rise significantly.
    """
    now = datetime(2023, 1, 1, 12, 15, 0, tzinfo=mock_state.local_tz)
    # Start time 15 minutes ago
    start_time = now - timedelta(minutes=15)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_verd = 5.0  # Cold start
    mock_state.kompressor_verification_start_t_unten = 30.0
    
    with pytest.MonkeyPatch.context() as m:
         class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
         m.setattr("safety_logic.datetime", MockDateTime)
         
         is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=5.2, current_t_unten=30.5
        )
         
         assert is_running is True
         assert error_msg is None

@pytest.mark.asyncio
async def test_verification_success_normal_drop(mock_state):
    """
    Test standard success case: significant temperature drop.
    """
    now = datetime(2023, 1, 1, 12, 15, 0, tzinfo=mock_state.local_tz)
    start_time = now - timedelta(minutes=15)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_verd = 20.0
    mock_state.kompressor_verification_start_t_unten = 30.0
    
    with pytest.MonkeyPatch.context() as m:
         class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
         m.setattr("safety_logic.datetime", MockDateTime)
         
         # Drop 2.0 deg (20 -> 18) > 1.5 threshold
         is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=18.0, current_t_unten=30.3
        )
         
         assert is_running is True
         assert error_msg is None

@pytest.mark.asyncio
async def test_verification_failure(mock_state):
    """
    Test failure: not cold enough, no drop.
    """
    now = datetime(2023, 1, 1, 12, 15, 0, tzinfo=mock_state.local_tz)
    start_time = now - timedelta(minutes=15)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_verd = 20.0 # Warm start
    mock_state.kompressor_verification_start_t_unten = 30.0
    
    with pytest.MonkeyPatch.context() as m:
         class MockDateTime(datetime):
             @classmethod
             def now(cls, tz=None):
                 return now
         m.setattr("safety_logic.datetime", MockDateTime)

         # No drop (20 -> 20), not cold start (< 15)
         is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=20.0, current_t_unten=30.0
        )
         
         assert is_running is False
         assert "Verdampfer: nur 0.0°C Abfall" in error_msg


@pytest.mark.asyncio
async def test_legionellen_erfolg_ohne_unten_delta(mock_state):
    """Empfehlung 3.4: Im Legionellenmodus genuegt der Verdampfer-Abfall.
    (unten saettigt bei ~47 C, Incident 11.09 -> KEIN Fehlalarm mehr.)"""
    mock_state.legionellen_temp_override = 60.0
    now = datetime(2023, 1, 1, 12, 15, 0, tzinfo=mock_state.local_tz)
    mock_state.kompressor_verification_start_time = now - timedelta(minutes=15)
    mock_state.kompressor_verification_start_t_verd = 20.0
    mock_state.kompressor_verification_start_t_unten = 47.25

    with pytest.MonkeyPatch.context() as m:
        class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        m.setattr("safety_logic.datetime", MockDateTime)

        # Verdampfer faellt sauber um 2.0K, unten steigt nur 0.1K (Saettigung)
        is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=18.0, current_t_unten=47.3
        )
        assert is_running is True
        assert error_msg is None


@pytest.mark.asyncio
async def test_legionellen_echter_stillstand_wird_weiter_erkannt(mock_state):
    """Auch im Legionellenmodus bleibt ein echter Kompressorstillstand sichtbar:
    ohne Verdampfer-Abfall -> Fehler (unten-Hinweis unterdrueckt, saettigt)."""
    mock_state.legionellen_temp_override = 60.0
    now = datetime(2023, 1, 1, 12, 15, 0, tzinfo=mock_state.local_tz)
    mock_state.kompressor_verification_start_time = now - timedelta(minutes=15)
    mock_state.kompressor_verification_start_t_verd = 20.0
    mock_state.kompressor_verification_start_t_unten = 47.25

    with pytest.MonkeyPatch.context() as m:
        class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        m.setattr("safety_logic.datetime", MockDateTime)

        # Drei aufeinanderfolgende Prüfungen ohne Verdampferabfall.
        # Ein einzelner Messwert ist absichtlich noch kein Stillstandsbeweis.
        results = []
        for _ in range(3):
            mock_state.kompressor_verification_last_check = None
            results.append(await verify_compressor_running(
                mock_state, None, current_t_verd=20.0, current_t_unten=47.3
            ))
        assert results[0][0] is True
        assert results[1][0] is True
        assert results[2][0] is False
        assert "Verdampfer" in results[2][1]
        assert "Unterer Fühler" not in results[2][1]


@pytest.mark.asyncio
async def test_unten_delta_gilt_als_betriebsbeweis(mock_state):
    """Ein klarer Temperaturanstieg am Boiler belegt aktive Waermeerzeugung."""
    now = datetime(2023, 1, 1, 12, 15, 0, tzinfo=mock_state.local_tz)
    mock_state.kompressor_verification_start_time = now - timedelta(minutes=15)
    mock_state.kompressor_verification_start_t_verd = 20.0
    mock_state.kompressor_verification_start_t_unten = 47.25

    with pytest.MonkeyPatch.context() as m:
        class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        m.setattr("safety_logic.datetime", MockDateTime)

        is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=18.0, current_t_unten=47.5
        )
        assert is_running is True
        assert error_msg is None


@pytest.mark.asyncio
async def test_steigender_verdampfer_widerlegt_positives_boilerdelta_nicht(mock_state):
    """Regression Log 20.09.: Verdampfer stieg, aber der Boiler stieg klar."""
    now = datetime(2023, 1, 1, 12, 15, 0, tzinfo=mock_state.local_tz)
    mock_state.kompressor_verification_start_time = now - timedelta(minutes=15)
    mock_state.kompressor_verification_start_t_verd = 20.0
    mock_state.kompressor_verification_start_t_unten = 30.0

    with pytest.MonkeyPatch.context() as m:
        class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        m.setattr("safety_logic.datetime", MockDateTime)

        is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=22.9, current_t_unten=31.3
        )
        assert is_running is True
        assert error_msg is None
