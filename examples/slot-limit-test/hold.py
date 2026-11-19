"""Explicit opt-in live slot test. Uses no external datasets or weights.

The supplied argument is a hold duration in seconds (default 180). A Kaggle
notebook can consume accelerator quota while it waits; use sparingly.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    if not 1 <= duration <= 600:
        raise ValueError("Duration must be 1..600 seconds")
    print(f"Holding Kaggle kernel for {duration}s (test only)", flush=True)
    started = now()
    time.sleep(duration)
    target = Path(os.environ.get("KGR_OUTPUT_DIR", ".")) / "slot_test.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"started_at": started, "ended_at": now(),
                                  "duration_seconds": duration, "ok": True}, indent=2),
                      encoding="utf-8")
    print("Slot test complete", flush=True)


if __name__ == "__main__":
    main()
