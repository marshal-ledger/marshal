"""The demo ledger builds from the golden fixtures, and its packaged source list stays in step with them."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from marshal_ledger import demo
from marshal_ledger.cli import main
from marshal_ledger.store import Store

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "tests" / "fixtures" / ".cache"
needs_fixtures = pytest.mark.skipif(not CACHE.is_dir(), reason="fixtures not fetched; run python scripts/fetch_fixtures.py")


def build_demo_script():
    spec = importlib.util.spec_from_file_location("build_demo", ROOT / "scripts" / "build_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_sources_match_the_golden_fixtures():
    packaged = json.loads((ROOT / "src" / "marshal_ledger" / "demo-sources.json").read_text())
    assert packaged == build_demo_script().demo_sources(), "run python scripts/build_demo.py --update-sources"
    assert all(s.repo and s.commit and s.files for s in demo.sources())


@needs_fixtures
def test_demo_builds(tmp_path):
    db = tmp_path / "demo.sqlite"
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_demo.py"), str(db)],
                         capture_output=True, text=True, cwd=tmp_path)
    assert run.returncode == 0, run.stderr[-2000:]
    assert len(Store(str(db)).agents()) >= 20


@needs_fixtures
def test_demo_command_reuses_fetched_definitions_and_keeps_existing_files(tmp_path, capsys):
    db = tmp_path / "demo.sqlite"
    assert main(["demo", str(db), "--fixtures", str(CACHE)]) == 0  # every file is cached, so nothing is downloaded
    assert "Downloading" not in capsys.readouterr().out
    assert len(Store(str(db)).agents()) >= 20
    assert main(["demo", str(db), "--fixtures", str(CACHE)]) == 1
    assert "--force" in capsys.readouterr().err
    assert main(["demo", str(db), "--fixtures", str(CACHE), "--force"]) == 0
