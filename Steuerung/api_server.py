"""Development entrypoint using the production FastAPI contract.

The former standalone mock app duplicated models and routes.  Keep only a
small bootstrap here so development and production cannot drift apart.
"""
import logging
import os

import uvicorn

import api as production_api
from config_manager import ConfigManager
from state import State

# Re-export the production request models for development imports.
ConfigUpdate = production_api.ConfigUpdate
ControlCommand = production_api.ControlCommand
app = production_api.app


def create_development_state():
    """Create a normal State and expose the same API contract as production."""
    config_manager = ConfigManager()
    config_manager.load_config()
    state = State(config_manager)
    production_api.init_api(
        state,
        {"enqueue_control": lambda command, params: None},
    )
    production_api.update_status_snapshot(state)
    return state


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_development_state()
    uvicorn.run(
        app,
        host=os.environ.get("WPS_DEV_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("WPS_DEV_API_PORT", "5000")),
        log_level="info",
    )
