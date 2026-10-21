"""Versioned Switchboard instructions; no session or scheduler mutation."""
BEGIN = '<!-- agent-tooling:switchboard:begin -->'
END = '<!-- agent-tooling:switchboard:end -->'


def render(root, name, home):
    return (root / 'switchboard' / name).read_bytes().replace(b'${HOME}', str(home).encode())


def guidance(original, root, home):
    text = original.decode()
    incoming = render(root, 'global-guidance.md', home).decode().rstrip()
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise RuntimeError('Malformed Switchboard guidance markers')
    if BEGIN in text:
        start = text.index(BEGIN)
        end = text.index(END) + len(END)
        if end < start:
            raise RuntimeError('Reversed Switchboard guidance markers')
        return (text[:start] + incoming + text[end:]).encode()
    anchor = '<!-- agent-tooling:manager:end -->'
    if anchor in text:
        position = text.index(anchor) + len(anchor)
        return (text[:position] + '\n\n' + incoming + text[position:]).encode()
    return (text + ('\n\n' if text else '') + incoming + '\n').encode()


def assets(root, home):
    return {str(home / '.codex/switchboard/SWITCHBOARD.md'):
            render(root, 'SWITCHBOARD.md', home)}
