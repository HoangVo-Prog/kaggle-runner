"""Inspect one mounted public Kaggle Dataset and one mounted Kaggle Model.

This smoke test uses Python's standard library: it does not download datasets,
fetch model weights, perform inference, or require GPU allocation.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

DATASET_REF = "uciml/iris"
MODEL_REF = "google/mobilenet-v2/tensorFlow2/130-224-classification/1"


def main() -> int:
    output = Path(os.environ.get("KGR_OUTPUT_DIR", ".")).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    report_file = output / "public_resources_report.json"
    report = {"status": "failed", "dataset_ref": DATASET_REF, "model_ref": MODEL_REF,
              "timestamp_utc": datetime.now(timezone.utc).isoformat()}
    try:
        dataset_paths = json.loads(os.environ.get("KGR_DATASET_PATHS", "{}"))
        model_paths = json.loads(os.environ.get("KGR_MODEL_PATHS", "{}"))
        if DATASET_REF not in dataset_paths:
            raise RuntimeError(f"Dataset {DATASET_REF!r} was not attached. Use --dataset {DATASET_REF}")
        if MODEL_REF not in model_paths:
            raise RuntimeError(f"Model {MODEL_REF!r} was not attached. Use --model {MODEL_REF}")
        dataset_dir = Path(dataset_paths[DATASET_REF])
        model_dir = Path(model_paths[MODEL_REF])
        csv_file = dataset_dir / "Iris.csv"
        if not csv_file.is_file():
            raise FileNotFoundError(f"Iris CSV missing: {csv_file}")
        if not model_dir.is_dir():
            raise FileNotFoundError(f"Model directory missing: {model_dir}")
        with csv_file.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        if not rows or "Species" not in rows[0]:
            raise RuntimeError("Iris.csv is empty or missing its Species column")
        model_files = sorted(str(f.relative_to(model_dir)) for f in islice(
            (p for p in model_dir.rglob("*") if p.is_file()), 30))
        if not model_files:
            raise RuntimeError(f"Mounted model contains no files: {model_dir}")
        species_counts = dict(sorted(Counter(r["Species"] for r in rows).items()))
        report.update({"status": "ok", "dataset_path": str(dataset_dir),
                       "dataset_rows": len(rows), "species_counts": species_counts,
                       "model_path": str(model_dir), "model_sample_files": model_files})
        with (output / "iris_species_counts.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["species", "count"])
            writer.writerows(species_counts.items())
        print(f"Dataset: {len(rows)} Iris samples; {len(species_counts)} species")
        print(f"Model: {model_dir} ({len(model_files)} sampled files)")
        return 0
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        report["traceback"] = traceback.format_exc()
        print(report["error"], file=sys.stderr)
        return 1
    finally:
        report_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
