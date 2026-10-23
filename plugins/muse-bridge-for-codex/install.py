#!/usr/bin/env python3
"""Register this checkout's controller in local Codex and install its CLI."""
import os
import argparse
import fcntl
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def controller_lock():
    state = Path.home() / '.local/state/muse-control'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (state / 'daemon.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit('Muse controller is running; installation refused without changing its runtime or settings. '
                         'Let managed sessions finish, stop the controller, then retry.') from None
    os.fchmod(lock.fileno(), 0o600)
    return lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider', action='store_true', help='Select the contributor/max custom provider in Codex')
    options = parser.parse_args()
    source = Path(__file__).resolve().parent
    root = Path.home() / '.local/share/muse-bridge-for-codex/runtime'
    with controller_lock():
        codex = shutil.which('codex')
        if not codex:
            raise SystemExit('Codex CLI is required.')
        if source != root:
            stage = root.with_name('.runtime.' + str(os.getpid()))
            shutil.copytree(source, stage, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'verification.json'))
            previous = root.with_name('.previous')
            if previous.exists():
                shutil.rmtree(previous)
            if root.exists():
                root.rename(previous)
            stage.rename(root)
        runtime = str(Path(sys.executable).resolve())
        script = str(root / 'bridge.py')
        destination = Path.home() / '.local/bin/muse-control'
        destination.parent.mkdir(parents=True, exist_ok=True)
        body = '#!/bin/sh\n# muse-control managed wrapper\nexec ' + shlex.join([runtime, script]) + ' "$@"\n'
        if destination.exists():
            existing = destination.read_text()
            if 'bridge.py' not in existing or 'muse-control' not in existing:
                raise SystemExit('An unrelated muse-control executable already exists; refusing to replace it.')
        config_home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        config_home.mkdir(parents=True, exist_ok=True)
        subprocess.run([codex, 'mcp', 'add', 'muse_control', '--', runtime, script, 'mcp'], check=True)
        temp = destination.with_name('.muse-control.install.' + str(os.getpid()))
        temp.write_text(body)
        temp.chmod(0o755)
        temp.replace(destination)
        if options.provider:
            sys.path.insert(0, str(root))
            import provider
            service = provider.prepare_install(root)
    if options.provider:
        # The new daemon must acquire the same lock; no old daemon was killed.
        provider.install(root, service)
    print('Installed Muse controller. Verify with: muse-control call muse_status')


if __name__ == '__main__':
    main()
