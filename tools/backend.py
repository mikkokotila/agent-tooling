"""Minimal PEP 517 backend using the standard wheel library; no network build."""
from pathlib import Path
import os, tarfile, tempfile
from wheel.wheelfile import WheelFile

ROOT = Path(__file__).resolve().parent.parent

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    version = (ROOT / 'VERSION').read_text().strip()
    filename = f'agent_tooling-{version}-py3-none-any.whl'
    target = Path(wheel_directory); target.mkdir(parents=True, exist_ok=True)
    with WheelFile(target / filename, 'w') as wheel:
        for path in sorted((ROOT / 'caller_pr_gates').glob('*')):
            if path.is_file() and path.suffix in {'.py', '.json'}:
                wheel.write(path, str(path.relative_to(ROOT)))
        for path in [ROOT / 'prompts.toml', ROOT / 'VERSION', *sorted((ROOT / 'config').glob('*'))]:
            wheel.write(path, 'agent_tooling_assets/' + str(path.relative_to(ROOT)))
        dist = f'agent_tooling-{version}.dist-info/'
        wheel.writestr(dist + 'WHEEL', 'Wheel-Version: 1.0\nGenerator: agent-tooling\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        wheel.writestr(dist + 'METADATA', f'Metadata-Version: 2.1\nName: agent-tooling\nVersion: {version}\nRequires-Python: >=3.13,<3.14\nSummary: Captured operational PR hooks\n')
    return filename

def build_sdist(sdist_directory, config_settings=None):
    version = (ROOT / 'VERSION').read_text().strip()
    filename = f'agent_tooling-{version}.tar.gz'
    destination = Path(sdist_directory); destination.mkdir(parents=True, exist_ok=True)
    from manage import source_files
    with tarfile.open(destination / filename, 'w:gz') as archive:
        for path in source_files(): archive.add(path, arcname=f'agent_tooling-{version}/' + str(path.relative_to(ROOT)))
    return filename
