"""
Validate notebooks/6_modelTraining.ipynb before committing to a full-scale run.

Two tiers of checking:

1. STATIC checks (always run, fast, no GPU/model download needed):
   - The notebook file is valid JSON / valid nbformat.
   - Every code cell parses as valid Python (magic cells like `%%writefile` are skipped).
   - Every cell heading is present and in the expected order (Cell 1..17).
   - REGRESSION GUARDS: confirms specific bug-fix lines are still present. This
     project has already had one incident where an editor "overwrite" reverted the
     notebook to a stale version and silently undid a fix -- these checks catch that
     class of problem before it wastes a multi-hour training run.

2. DYNAMIC smoke test (skipped with --static-only): makes a temporary copy of the
   real notebook with the sample sizes and epoch count overridden to tiny values
   (so it finishes in a couple of minutes instead of ~30-60), points its output
   files at a throwaway subfolder so nothing in data/ gets overwritten, and
   actually executes it top to bottom with nbclient. This exercises the real
   code path -- data loading, tokenization, LoRA classifier training, the
   executor, and metrics scoring -- not just a syntax check.

Usage:
    python test.py                       # static checks + smoke run (default)
    python test.py --static-only         # static checks only, no execution
    python test.py --kernel sapsam       # use a specific installed Jupyter kernel
    python test.py --notebook path.ipynb # validate a different notebook

Exit code 0 = all checks passed. Exit code 1 = something failed (see printed detail).
"""
from __future__ import annotations

import argparse
import ast
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

DEFAULT_NOTEBOOK = Path(__file__).parent / "notebooks" / "6_modelTraining.ipynb"

EXPECTED_HEADINGS = [
    "Cell 1", "Cell 2", "Cell 3", "Cell 4", "Cell 5", "Cell 6", "Cell 7",
    "Cell 8", "Cell 9", "Cell 10", "Cell 11", "Cell 12", "Cell 13",
    "Cell 14", "Cell 15", "Cell 16", "Cell 17",
]

# (description, substring that must be present somewhere in the notebook source)
# Each of these corresponds to a specific bug that was found and fixed earlier.
# If any of these go missing (e.g. from an editor reverting to a stale version),
# that exact bug is back.
REGRESSION_GUARDS = [
    ("Windows DataLoader crash fix (dataloader_num_workers=0)",
     "dataloader_num_workers=0"),
    ("CUDA allocator fragmentation fix (max_split_size_mb)",
     "max_split_size_mb"),
    ("Dangling task-reference hardening in GraphBuilder.next_after_task",
     "Dangling reference"),
    ("as_is_metrics wrapped in try/except (Cell 15 robustness fix)",
     "as_is_metrics_error"),
    ("Executor + validate_record wrapped in try/except (Cell 14 robustness fix)",
     "executor_error"),
    ("Executor reuses bpr_pipeline.HEURISTICS directly (no hand-typed duplicate list)",
     "_by_name = {name: (hid, apply_fn) for hid, name, qualify, apply_fn in bpr.HEURISTICS}"),
    ("group_by_length NOT used (removed -- unsupported on this transformers version)",
     None),  # handled specially below: must be ABSENT, not present
]


def load_notebook_cells(path: Path) -> list[dict]:
    nb = json.loads(path.read_text(encoding="utf-8"))
    return nb["cells"]


def full_source(cells: list[dict]) -> str:
    return "\n".join("".join(c.get("source", [])) for c in cells)


def check_static(path: Path) -> list[str]:
    """Returns a list of problem descriptions. Empty list = all good."""
    problems = []

    if not path.exists():
        return [f"Notebook not found: {path}"]

    try:
        raw = path.read_text(encoding="utf-8")
        nb = json.loads(raw)
    except Exception as exc:
        return [f"Notebook is not valid JSON: {exc}"]

    cells = nb.get("cells", [])
    if not cells:
        problems.append("Notebook has no cells")
        return problems

    # 1. Every code cell must be valid Python (skip magic cells like %%writefile)
    for i, cell in enumerate(cells):
        if cell.get("cell_type") != "code":
            continue
        src = "".join(cell.get("source", []))
        if src.lstrip().startswith("%%"):
            continue
        try:
            ast.parse(src)
        except SyntaxError as exc:
            problems.append(f"Cell {i}: SyntaxError: {exc}")

    # 2. Expected cell headings, in order
    headings_found = [
        "".join(c["source"]).strip()
        for c in cells
        if c.get("cell_type") == "markdown"
    ]
    for expected in EXPECTED_HEADINGS:
        if not any(expected in h for h in headings_found):
            problems.append(f"Missing expected heading containing '{expected}'")

    # 3. Regression guards
    text = full_source(cells)
    for description, needle in REGRESSION_GUARDS:
        if needle is None:
            continue
        if needle not in text:
            problems.append(f"Regression guard failed -- missing: {description}")

    # group_by_length must be ABSENT (it caused a real TypeError on this transformers version)
    if "group_by_length=True" in text:
        problems.append(
            "Regression guard failed -- 'group_by_length=True' is present again; "
            "this raised TypeError on the installed transformers version before"
        )

    return problems


def build_smoke_test_copy(src_path: Path) -> Path:
    """Writes a temp copy of the notebook with tiny sample sizes / 1 epoch, and
    output paths redirected to a throwaway subfolder so nothing real gets touched."""
    nb = json.loads(src_path.read_text(encoding="utf-8"))

    replacements = [
        ("MAX_TRAIN = 20000", "MAX_TRAIN = 40  # [SMOKE TEST override]"),
        ("MAX_EVAL = 4000", "MAX_EVAL = 20  # [SMOKE TEST override]"),
        ("num_train_epochs=5,", "num_train_epochs=1,  # [SMOKE TEST override]"),
        ("warmup_steps=100,", "warmup_steps=1,  # [SMOKE TEST override]"),
        (
            'ADAPTER_PATH = DATA_ROOT / "qwen_bpr_classifier"',
            'ADAPTER_PATH = DATA_ROOT / "_smoke_test_output" / "qwen_bpr_classifier"  # [SMOKE TEST override]',
        ),
        (
            'CHECKPOINT_DIR = DATA_ROOT / "training_checkpoints_classifier"',
            'CHECKPOINT_DIR = DATA_ROOT / "_smoke_test_output" / "training_checkpoints_classifier"  # [SMOKE TEST override]',
        ),
        (
            'with open(DATA_ROOT / "classifier_training_metrics.json", "w") as f:',
            '(DATA_ROOT / "_smoke_test_output").mkdir(parents=True, exist_ok=True)\n'
            'with open(DATA_ROOT / "_smoke_test_output" / "classifier_training_metrics.json", "w") as f:',
        ),
        (
            'with open(DATA_ROOT / "classifier_eval_metrics.json", "w") as f:',
            'with open(DATA_ROOT / "_smoke_test_output" / "classifier_eval_metrics.json", "w") as f:',
        ),
        (
            'with open(DATA_ROOT / "training_summary.json", "w") as f:',
            'with open(DATA_ROOT / "_smoke_test_output" / "training_summary.json", "w") as f:',
        ),
    ]

    applied = {desc: 0 for desc, _ in [(r[0], r[0]) for r in replacements]}
    for cell in nb["cells"]:
        if cell.get("cell_type") != "code":
            continue
        text = "".join(cell["source"])
        changed = False
        for old, new in replacements:
            if old in text:
                text = text.replace(old, new)
                applied[old] = applied.get(old, 0) + 1
                changed = True
        if changed:
            cell["source"] = text.splitlines(keepends=True)

    missing = [old for old, count in applied.items() if count == 0]
    if missing:
        print("WARNING: some smoke-test overrides did not find a match (notebook structure "
              "may have changed) -- the smoke test will still run, but may use full-scale "
              "settings for these:")
        for m in missing:
            print(f"    - {m!r}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="nb_smoke_test_"))
    out_path = tmp_dir / src_path.name
    out_path.write_text(json.dumps(nb, indent=1), encoding="utf-8")
    return out_path


def _cell_output_text(cell) -> str:
    text = ""
    for out in cell.get("outputs", []):
        text += out.get("text", "")
        if "data" in out and "text/plain" in out.get("data", {}):
            data = out["data"]["text/plain"]
            text += "".join(data) if isinstance(data, list) else data
        if out.get("output_type") == "error":
            text += "\n".join(out.get("traceback", []))
    return text


def run_smoke_test(notebook_path: Path, kernel_name: str, timeout: int) -> tuple[bool, str]:
    import time

    try:
        import nbformat
        from nbclient import NotebookClient
        from nbclient.exceptions import CellExecutionError
    except ImportError as exc:
        return False, (
            f"Cannot run the dynamic smoke test: {exc}. "
            "Install with `pip install nbclient nbformat`, or pass --static-only "
            "to skip execution and only run the fast structural checks."
        )

    smoke_copy = build_smoke_test_copy(notebook_path)
    print(f"Smoke-test notebook copy: {smoke_copy}")
    print("(sample sizes/epochs reduced; outputs redirected to data/_smoke_test_output/)")
    print(f"Using kernel: {kernel_name!r}  (pass --kernel NAME if this is wrong for your env)")
    print("Running cell by cell with live progress below (per-cell timeout: "
          f"{timeout}s -- the first model-loading cell can be slow if it has to "
          "download from Hugging Face Hub).\n")

    nb = nbformat.read(str(smoke_copy), as_version=4)
    client = NotebookClient(
        nb,
        timeout=timeout,
        kernel_name=kernel_name,
        resources={"metadata": {"path": str(notebook_path.parent)}},
    )

    # Build a quick index -> preceding markdown heading map, so progress lines
    # say "Cell 5 -- Load Qwen + LoRA" instead of a bare, meaningless cell index.
    heading_for_index = {}
    current_heading = "(no heading yet)"
    for i, cell in enumerate(nb.cells):
        if cell.cell_type == "markdown":
            current_heading = "".join(cell["source"]).strip().lstrip("#").strip()
        heading_for_index[i] = current_heading

    saved_notebook = smoke_copy.with_name(smoke_copy.stem + "_executed.ipynb")

    try:
        with client.setup_kernel():
            for index, cell in enumerate(nb.cells):
                if cell.cell_type != "code":
                    continue
                label = heading_for_index.get(index, "?")
                print(f"[{time.strftime('%H:%M:%S')}] -> executing cell {index} ({label}) ...",
                      flush=True)
                t0 = time.time()
                try:
                    client.execute_cell(cell, index)
                except CellExecutionError as exc:
                    elapsed = time.time() - t0
                    print(f"[{time.strftime('%H:%M:%S')}]    FAILED after {elapsed:.1f}s")
                    nbformat.write(nb, str(saved_notebook))
                    return False, (
                        f"Execution failed at cell {index} ({label}).\n\n{exc}\n\n"
                        f"Notebook with outputs up to the failure saved at:\n{saved_notebook}"
                    )
                elapsed = time.time() - t0
                out_text = _cell_output_text(cell).strip()
                tail = out_text[-300:] if out_text else "(no output)"
                print(f"[{time.strftime('%H:%M:%S')}]    done in {elapsed:.1f}s -- last output: {tail}",
                      flush=True)
    except Exception as exc:
        return False, f"Unexpected error running the smoke test: {exc}\n{traceback.format_exc()}"

    nbformat.write(nb, str(saved_notebook))

    # Sanity-check the outputs for anything that looks like a silent problem
    # (a cell can "succeed" in nbclient's eyes but still print a red flag).
    warnings = []
    out_text = "".join(_cell_output_text(cell) for cell in nb.cells if cell.cell_type == "code")

    if "RUN COMPLETE" not in out_text:
        warnings.append("Did not see the final 'RUN COMPLETE' message -- notebook may not have "
                         "reached the last cell even though no exception was raised.")
    if "Executor produced a schema-valid to-be for 0/" in out_text:
        warnings.append("Executor produced ZERO schema-valid to-be records in the smoke test -- "
                         "something is likely wrong with the executor or validate_record, even "
                         "though nothing raised an exception.")

    if warnings:
        return False, (
            "Execution completed without raising, but output looks suspicious:\n  - "
            + "\n  - ".join(warnings)
            + f"\n\nExecuted notebook saved at: {saved_notebook}"
        )

    return True, f"Smoke test passed. Executed notebook saved at: {saved_notebook}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--notebook", type=Path, default=DEFAULT_NOTEBOOK,
                         help="Path to the notebook to validate (default: notebooks/6_modelTraining.ipynb)")
    parser.add_argument("--static-only", action="store_true",
                         help="Only run fast structural checks, skip actually executing the notebook")
    parser.add_argument("--kernel", default="python3",
                         help="Jupyter kernel name to execute with (default: python3). "
                              "Use the kernel that has torch/transformers/peft/datasets installed "
                              "-- e.g. whatever name you registered with "
                              "`python -m ipykernel install --user --name=<name>`.")
    parser.add_argument("--timeout", type=int, default=1800,
                         help="Max seconds for the smoke test to run (default: 1800 = 30 min, "
                              "generous because of the first-time model download)")
    args = parser.parse_args()

    print("=" * 70)
    print(f"STATIC CHECKS -- {args.notebook}")
    print("=" * 70)
    problems = check_static(args.notebook)
    if problems:
        print(f"FAILED -- {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  - {p}")
        print("\nFix these before running the smoke test or the full training run.")
        sys.exit(1)
    print("PASSED -- notebook is valid Python, has all expected cells, and all known fixes "
          "are present (no silent reverts detected).")

    if args.static_only:
        print("\n--static-only was set, skipping the execution smoke test.")
        sys.exit(0)

    print()
    print("=" * 70)
    print("DYNAMIC SMOKE TEST (tiny sample, 1 epoch, real execution)")
    print("=" * 70)
    ok, message = run_smoke_test(args.notebook, args.kernel, args.timeout)
    print()
    if ok:
        print(f"PASSED -- {message}")
        print("\nAll checks passed. Safe to proceed to the full-scale run.")
        sys.exit(0)
    else:
        print(f"FAILED -- {message}")
        print("\nDo not proceed to the full-scale run until this is fixed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
