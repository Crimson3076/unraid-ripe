import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("script", ["tests/test_dashboard_ui.cjs", "tests/test_app_page_ui.cjs"])
def test_dashboard_ui_script(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    env = {**os.environ, "RIPE_PYTHON": sys.executable}
    probe = subprocess.run([node, "-e", "require(process.env.RIPE_JSDOM||'jsdom')"], cwd=ROOT, env=env, capture_output=True)
    if probe.returncode:
        pytest.skip("jsdom is not installed")
    result = subprocess.run([node, script], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
