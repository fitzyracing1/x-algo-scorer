"""Calibration report on synthetic submissions."""

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "calibration_report.py"


def run_report(directory: Path) -> str:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--dir", str(directory)],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def write_submission(directory: Path, name: str, record: dict):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yml").write_text(yaml.safe_dump(record))


def test_empty_dir_reports_nothing_to_calibrate(tmp_path):
    out = run_report(tmp_path)
    assert "Nothing to calibrate yet" in out


def test_overconfident_guess_is_called_out(tmp_path):
    write_submission(tmp_path, "one", {
        "text": "post",
        "predictions": {"favorite": 0.10, "reply": 0.001},
        "actual": {"impressions": 1000, "favorites": 20, "replies": 1,
                   "reposts": 0, "quotes": 0},
    })
    out = run_report(tmp_path)
    assert "1 with real outcomes" in out
    # guessed 0.10 vs actual 0.02 -> 5x high
    assert "5.0" in out and "favorite" in out
    assert "high" in out


def test_submission_without_impressions_is_skipped(tmp_path):
    write_submission(tmp_path, "pending", {
        "text": "post",
        "predictions": {"favorite": 0.1},
        "actual": {"impressions": 0, "favorites": 0},
    })
    out = run_report(tmp_path)
    assert "0 with real outcomes" in out
