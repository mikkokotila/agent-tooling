"""Run the publication recovery regressions with the captured offline tool environment."""
import os
import subprocess
import tempfile

from manage import ROOT, build, verification_git

python = build()
with tempfile.TemporaryDirectory(prefix='limen-docs-recovery-check-') as state:
    environment = dict(os.environ, PYTHONPATH=str(ROOT), CALLER_PR_GATES_STATE_DIR=state,
                       CALLER_PR_GATES_PROMPTS_STATE_DIR=state,
                       CALLER_PR_GATES_PROMPTS_FILE=str(ROOT / 'prompts.toml'))
    verification_git(environment, state)
    subprocess.run([str(python), '-m', 'pytest', 'tests/test_limen_docs_recovery.py', '-q', '-x', '--tb=short'],
                   cwd=ROOT, env=environment, check=True, timeout=90)
