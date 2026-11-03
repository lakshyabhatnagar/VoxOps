import json
import logging


# Why: Make operational events usable without logging source or request contents.
# How: Emit only caller-supplied event codes and generated run identifiers as JSON.
def event(code: str, run_id: str) -> None:
    logging.getLogger("voxops").info(json.dumps({"event": code, "run_id": run_id}))
