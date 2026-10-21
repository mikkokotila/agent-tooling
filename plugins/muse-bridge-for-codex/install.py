#!/usr/bin/env python3
"""Register this checkout's controller in local Codex and install its CLI."""
import os
import argparse
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider', action='store_true', help='Select the contributor/max custom provider in Codex')
    options = parser.parse_args()
    source = Path(__file__).resolve().parent
    root = Path.home() / '.local/share/muse-bridge-for-codex/runtime'
    if source != root:
        stage = root.with_name('.runtime.' + str(os.getpid()))
        shutil.copytree(source, stage, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'verification.json'))
        previous = root.with_name('.previous')
        if previous.exists():
            shutil.rmtree(previous)
        if root.exists():
            root.rename(previous)
        stage.rename(root)
    codex = shutil.which('codex')
    if not codex:
        raise SystemExit('Codex CLI is required.')
    runtime = str(Path(sys.executable).resolve())
    script = str(root / 'bridge.py')
    destination = Path.home() / '.local/bin/muse-control'
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = '#!/bin/sh\n# muse-control managed wrapper\nexec ' + shlex.join([runtime, script]) + ' "$@"\n'
    if destination.exists():
        existing = destination.read_text()
        if 'bridge.py' not in existing or 'muse-control' not in existing:
            raise SystemExit('An unrelated muse-control executable already exists; refusing to replace it.')
    subprocess.run([codex, 'mcp', 'add', 'muse_control', '--', runtime, script, 'mcp'], check=True)
    temp = destination.with_name('.muse-control.install.' + str(os.getpid()))
    temp.write_text(body)
    temp.chmod(0o755)
    temp.replace(destination)
    print('Installed Muse controller. Verify with: muse-control call muse_status')
    if options.provider:
        sys.path.insert(0, str(root))
        import provider
        provider.install(root)


if __name__ == '__main__':
    main()
