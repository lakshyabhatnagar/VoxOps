import json
import logging


# Records an application event without including request contents.
# Writes the event name and run identifiers as JSON.
def event(code: str, run_id: str) -> None:
    logging.getLogger("voxops").info(json.dumps({"event": code, "run_id": run_id}))
