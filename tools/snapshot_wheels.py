"""Capture installed distribution files as offline wheels, without credentials."""
from importlib import metadata
from pathlib import Path
import base64, csv, hashlib, io, json, sys, zipfile

destination = Path(sys.argv[1])
destination.mkdir(parents=True, exist_ok=True)
names = sys.argv[2:]
result = []
for name in names:
    dist = metadata.distribution(name)
    files = {}
    for file in dist.files or []:
        rel = str(file)
        if rel.startswith('../') or '__pycache__/' in rel or rel.endswith(('.pyc', '/direct_url.json', '/RECORD')):
            continue
        path = Path(dist.locate_file(file))
        if path.is_file(): files[rel] = path.read_bytes()
    meta = next(v.decode() for k, v in files.items() if k.endswith('.dist-info/WHEEL'))
    tag = next(line[5:].strip() for line in meta.splitlines() if line.startswith('Tag: '))
    filename = dist.metadata['Name'].replace('-', '_') + '-' + dist.version + '-' + tag + '.whl'
    record = next(k.rsplit('/', 1)[0] + '/RECORD' for k in files if k.endswith('.dist-info/METADATA'))
    rows = [[rel, 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode(), str(len(data))]
            for rel, data in sorted(files.items())]
    rows.append([record, '', ''])
    stream = io.StringIO(); csv.writer(stream, lineterminator='\n').writerows(rows)
    files[record] = stream.getvalue().encode()
    path = destination / filename
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for rel, data in sorted(files.items()):
            info = zipfile.ZipInfo(rel, (2026, 10, 10, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED; info.external_attr = 0o644 << 16
            z.writestr(info, data)
    result.append({'file': filename, 'name': dist.metadata['Name'], 'version': dist.version,
                   'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
(destination / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n')
