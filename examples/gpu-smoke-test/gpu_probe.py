"""Small Kaggle GPU validation job. No credentials, data, or local paths needed.

Writes its outputs to KGR_OUTPUT_DIR (provided by kaggle-runner), or the
current working directory for local debugging. No network access required.
"""
from __future__ import annotations

import csv
import json
import os
import platform
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    out = Path(os.environ.get("KGR_OUTPUT_DIR", ".")).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "gpu_smoke_result.json"
    csv_path = out / "gpu_devices.csv"
    log_path = out / "gpu_smoke.log"
    report: dict = {
        "started_at_utc": now(),
        "status": "failed",
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "devices": [],
    }
    errors = []
    lines = []

    def log(message: str) -> None:
        print(message, flush=True)
        lines.append(message)

    try:
        smi = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader"],
            check=False, text=True, capture_output=True, timeout=15,
        )
        report["nvidia_smi"] = {
            "returncode": smi.returncode,
            "output": smi.stdout.strip(),
            "error": smi.stderr.strip()[:500],
        }
        log(f"nvidia-smi result: {smi.returncode}")
        if smi.stdout.strip():
            log(smi.stdout.strip())
    except (OSError, subprocess.TimeoutExpired) as exc:
        report["nvidia_smi"] = {"error": repr(exc)}
        log(f"nvidia-smi unavailable: {exc}")

    try:
        import torch  # Kaggle's default Python GPU image includes PyTorch.
        report["torch_version"] = torch.__version__
        report["cuda_available"] = bool(torch.cuda.is_available())
        report["cuda_device_count"] = int(torch.cuda.device_count()) if report["cuda_available"] else 0
        log(f"torch={torch.__version__}, cuda={report['cuda_available']}, "
            f"device_count={report['cuda_device_count']}")

        if not report["cuda_available"]:
            raise RuntimeError("No CUDA GPU detected. Check Kaggle GPU access and allocation.")

        # Modest workload (<100MB per GPU), not a long-running benchmark.
        for gpu_index in range(report["cuda_device_count"]):
            torch.cuda.set_device(gpu_index)
            device = torch.device(f"cuda:{gpu_index}")
            torch.manual_seed(42 + gpu_index)
            name = torch.cuda.get_device_name(gpu_index)
            size = 512
            left = torch.randn((size, size), device=device)
            right = torch.randn((size, size), device=device)
            for _ in range(2):
                _ = left @ right
            torch.cuda.synchronize(device)
            start = time.perf_counter()
            checksum = 0.0
            for _ in range(8):
                product = left @ right
                checksum += float(product.mean().item())
            torch.cuda.synchronize(device)
            elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
            device_record = {
                "index": gpu_index,
                "name": name,
                "matrix_size": size,
                "iterations": 8,
                "elapsed_ms": elapsed_ms,
                "checksum": round(checksum, 6),
            }
            report["devices"].append(device_record)
            log(f"GPU {gpu_index} ({name}): 8 matmuls {size}x{size} in {elapsed_ms} ms, checksum={checksum:.6f}")
            del left, right, product
            torch.cuda.empty_cache()

        report["status"] = "ok"
        log("GPU smoke test PASSED")
    except Exception as exc:
        errors.append({"type": type(exc).__name__, "message": str(exc),
                       "traceback": traceback.format_exc()[-4000:]})
        log(f"GPU smoke test FAILED: {type(exc).__name__}: {exc}")
    finally:
        report["ended_at_utc"] = now()
        report["errors"] = errors
        with csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["index", "name", "matrix_size", "iterations", "elapsed_ms", "checksum"])
            writer.writeheader()
            writer.writerows(report["devices"])
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log(f"Saved results: {report_path.name}, {csv_path.name}, {log_path.name}")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    if main() != 0:
        raise RuntimeError("GPU smoke test did not pass; inspect gpu_smoke_result.json")
