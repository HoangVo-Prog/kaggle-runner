"""CPU-only offline scientific smoke test. Execution does not require internet."""
import json
import os
from pathlib import Path
import time

from utils import score


def main():
    t0 = time.monotonic()
    parameters = json.loads(os.environ.get("KGR_PARAMETERS_JSON", "{}"))
    output = Path(os.environ.get("KGR_OUTPUT_DIR", "."))
    output.mkdir(parents=True, exist_ok=True)
    accuracy = score(parameters.get("prediction", [1, 0, 1, 0]), parameters.get("gold", [1, 0, 0, 0]))
    record = {
        "schema_version": "1.0", "experiment_id": os.environ["KGR_EXPERIMENT_ID"],
        "attempt_id": os.environ["KGR_ATTEMPT_ID"], "status": "succeeded",
        "seed": int(parameters["seed"]), "dataset": {"ref": "example/synthetic", "split": "validation"},
        "protocol_id": "smoke-v1", "source_sha256": os.environ["KGR_SOURCE_SHA256"],
        "metrics": {"accuracy": {"value": accuracy, "unit": "fraction", "higher_is_better": True}},
        "samples_evaluated": len(parameters["gold"]), "runtime_seconds": time.monotonic() - t0,
        "artifacts": []
    }
    (output / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
