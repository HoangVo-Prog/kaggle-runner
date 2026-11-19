"""Minimal CPU smoke test for a real Kaggle notebook (no credentials or datasets)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    output = Path(os.environ.get("KGR_OUTPUT_DIR", "."))
    output.mkdir(parents=True, exist_ok=True)
    report = {"ok": True, "task": "kgr_multi_account_live_smoke",
              "completed_at_utc": datetime.now(timezone.utc).isoformat()}
    (output / "team_smoke_result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Team smoke completed", flush=True)


if __name__ == "__main__":
    main()
