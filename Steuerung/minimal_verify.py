import sys
import os
from datetime import datetime, timedelta
import pytz
from unittest.mock import MagicMock, patch

# Add Steuerung to path
sys.path.append(os.path.abspath(os.path.join(os.getcwd())))

# Mock dependencies before importing main
sys.modules['RPi.GPIO'] = MagicMock()
sys.modules['smbus2'] = MagicMock()
sys.modules['uvicorn'] = MagicMock()
sys.modules['aiohttp'] = MagicMock()
sys.modules['aiofiles'] = MagicMock()
sys.modules['fastapi'] = MagicMock()
sys.modules['pytz'] = MagicMock() # We already use pytz, but let's be safe if main imports it differently
import pytz # restore real pytz for our test needs

# Now import the functions to test
try:
    from main import set_kompressor_status, handle_day_transition
    from utils import safe_timedelta
    import main
    main.hardware_manager = MagicMock()
except ImportError as e:
    print(f"Import Error: {e}")
    sys.exit(1)

def test_pause_logic():
    print("Testing Pause Logic...")
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    state.control = MagicMock()
    state.stats = MagicMock()
    state.sensors = MagicMock()
    
    state.control.kompressor_ein = False
    state.stats.last_compressor_on_time = None
    
    # CASE: Already OFF, should not update last_compressor_off_time
    fixed_off_time = datetime.now(state.local_tz) - timedelta(hours=5)
    state.stats.last_compressor_off_time = fixed_off_time
    
    import asyncio
    async def run():
        await set_kompressor_status(state, False)
        if state.stats.last_compressor_off_time == fixed_off_time:
            print("  SUCCESS: last_compressor_off_time not reset when already OFF")
        else:
            print(f"  FAILED: last_compressor_off_time was changed to {state.stats.last_compressor_off_time}")

        # CASE: Switching from ON to OFF
        state.control.kompressor_ein = True
        state.stats.last_compressor_on_time = datetime.now(state.local_tz) - timedelta(minutes=15)
        state.stats.total_runtime_today = timedelta()
        
        await set_kompressor_status(state, False)
        if not state.control.kompressor_ein and state.stats.total_runtime_today.total_seconds() > 0:
            print(f"  SUCCESS: Runtime calculated on OFF: {state.stats.total_runtime_today}")
        else:
            print(f"  FAILED: Runtime not calculated or state not updated")

    asyncio.run(run())

def test_midnight():
    print("Testing Midnight Logic...")
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    state.control.kompressor_ein = True
    
    # 23:45 yesterday
    yesterday = datetime.now(state.local_tz) - timedelta(days=1)
    on_time = yesterday.replace(hour=23, minute=45, second=0, microsecond=0)
    state.stats.last_compressor_on_time = on_time
    state.stats.total_runtime_today = timedelta(minutes=10) # already had some runtime
    state.stats.last_day = yesterday.date()

    # Current time: 00:05 today
    now = datetime.now(state.local_tz).replace(hour=0, minute=5, second=0, microsecond=0)
    
    handle_day_transition(state, now)
    
    # check that on_time was moved to midnight
    expected_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if state.stats.last_compressor_on_time == expected_midnight:
        print("  SUCCESS: last_compressor_on_time moved to midnight")
    else:
        print(f"  FAILED: last_compressor_on_time is {state.stats.last_compressor_on_time}")
    
    if state.stats.total_runtime_today == timedelta():
        print("  SUCCESS: total_runtime_today reset for new day")
    else:
        print(f"  FAILED: total_runtime_today is {state.stats.total_runtime_today}")

if __name__ == "__main__":
    test_pause_logic()
    test_midnight()
