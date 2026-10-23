"""Versioned manager documents; no task-state or scheduler mutation."""
import json
import re

BEGIN = '<!-- agent-tooling:manager:begin -->'
END = '<!-- agent-tooling:manager:end -->'


def render(root, name, home):
    return (root / 'manager' / name).read_bytes().replace(b'${HOME}', str(home).encode())


def guidance(original, root, home):
    text = original.decode()
    legacy = json.loads(render(root, 'legacy-guidance.json', home))
    for heading, expected in legacy.items():
        matches = list(re.finditer(r'^' + re.escape(heading) + r'\s*$', text, re.M))
        if len(matches) > 1:
            raise RuntimeError('Duplicated manager guidance; reconcile before installation')
        if not matches:
            continue
        start = matches[0].start()
        following = re.search(r'^## ', text[matches[0].end():], re.M)
        end = matches[0].end() + following.start() if following else len(text)
        block = text[start:end].strip()
        # The first manually installed Claude invitation named the then-manager.
        # Match only that exact legacy wording, never retain a session ID in Git.
        normalized = re.sub(
            r'existing manager chat `[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}`',
            'existing human-appointed manager recorded in `'
            + str(home) + '/.codex/work-manager/state.json → manager_session`', block)
        if normalized != expected:
            raise RuntimeError('Edited legacy manager guidance; reconcile before installation')
        text = text[:start] + text[end:]
    incoming = render(root, 'global-guidance.md', home).decode().rstrip()
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise RuntimeError('Malformed manager guidance markers')
    if BEGIN in text:
        start = text.index(BEGIN)
        end = text.index(END) + len(END)
        if end < start:
            raise RuntimeError('Reversed manager guidance markers')
        return (text[:start] + incoming + text[end:]).encode()
    # Insert beside the existing managed PR block, preserving personal prefixes
    # and suffixes. Installation never rewrites those unrelated instructions.
    anchor = '<!-- caller-pr-gates:end -->'
    if anchor in text:
        position = text.index(anchor) + len(anchor)
        return (text[:position] + '\n\n' + incoming + text[position:]).encode()
    return (text + ('\n\n' if text else '') + incoming + '\n').encode()


def assets(root, home):
    base = home / '.codex/work-manager'
    files = {'MANAGER.md': 'MANAGER.md', 'REQUESTS.md': 'requests/README.md',
             'heartbeat.txt': 'heartbeat.txt', 'turn_usage.py': 'turn_usage.py'}
    return {str(base / target): render(root, source, home)
            for source, target in files.items()}
