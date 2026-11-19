# Native Kaggle notebook smoke test

From the repository root (after `kgr auth init`):

```bash
kgr run ./examples/notebook-smoke-test/hello.ipynb --gpu cpu --dry-run
kgr run ./examples/notebook-smoke-test/hello.ipynb --gpu cpu --wait --fetch
kgr report --refresh
```

The second command submits a **real Kaggle notebook**, waits for completion, then downloads `notebook_result.json` into the runner's user-local output directory. `--dry-run` does not contact Kaggle.
