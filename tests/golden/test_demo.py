"""The demo ledger builds from the golden fixtures."""

import subprocess
import sys
from pathlib import Path

import pytest

from marshal_ledger.store import Store

ROOT = Path(__file__).resolve().parents[2]


def test_demo_builds(tmp_path):
    if not (ROOT / "tests" / "fixtures" / ".cache").is_dir():
        pytest.skip("fixtures not fetched; run python scripts/fetch_fixtures.py")
    db = tmp_path / "demo.sqlite"
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_demo.py"), str(db)],
                         capture_output=True, text=True, cwd=tmp_path)
    assert run.returncode == 0, run.stderr[-2000:]
    assert len(Store(str(db)).agents()) >= 20
