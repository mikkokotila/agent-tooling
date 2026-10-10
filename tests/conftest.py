"""Keep all fixture state local; explicitly retain the captured quota defect."""
from pathlib import Path
import json, pytest

ROOT = Path(__file__).resolve().parent.parent
KNOWN = set(json.loads((ROOT / 'release/runtime-manifest.json').read_text())['known_defects'])

def pytest_collection_modifyitems(items):
    for item in items:
        if item.nodeid in KNOWN:
            item.add_marker(pytest.mark.xfail(strict=True, reason='Explicit release defect'))

@pytest.fixture(autouse=True)
def isolated_prompt_state(tmp_path, monkeypatch):
    monkeypatch.setenv('CALLER_PR_GATES_PROMPTS_STATE_DIR',str(tmp_path/'prompt-cache'))
    monkeypatch.setenv('CALLER_PR_GATES_STATE_DIR',str(tmp_path/'hook-state'))
