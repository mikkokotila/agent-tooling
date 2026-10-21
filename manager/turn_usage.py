"""Read recorded usage for one verified, completed Codex turn; never estimate."""
import argparse
import json
from pathlib import Path
import sqlite3

LIMIT = 8 * 1024 * 1024
COUNTERS = ('input_tokens', 'cached_input_tokens', 'output_tokens',
            'reasoning_output_tokens', 'total_tokens')
UNKNOWN = 'Last completed turn: unavailable (no verified usage).'


def completed_usage(path):
    """A bounded tail needs the start, preceding cumulative count and completion."""
    prior = active = result = None
    with Path(path).open('rb') as stream:
        offset = max(0, stream.seek(0, 2) - LIMIT)
        stream.seek(offset)
        if offset:
            stream.readline()
        for line in stream:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if event.get('type') != 'event_msg':
                continue
            item = event.get('payload', {})
            kind = item.get('type')
            if kind == 'task_started':
                active = (item.get('turn_id'), prior, False)
            elif kind == 'token_count':
                usage = (item.get('info') or {}).get('total_token_usage')
                if not isinstance(usage, dict):
                    continue
                if not all(type(usage.get(k)) is int and usage[k] >= 0 for k in COUNTERS):
                    active = None
                    prior = None
                    continue
                if prior and any(usage[k] < prior[k] for k in COUNTERS):
                    active = None
                if active:
                    active = (active[0], active[1], True)
                prior = usage
            elif kind == 'task_complete':
                # Do not return an earlier turn when the latest one lacks proof.
                result = None
                if not active or not active[0] or not active[2] or active[0] != item.get('turn_id'):
                    continue
                base = active[1]
                if base is None or prior is None:
                    continue
                delta = {k: prior[k] - base[k] for k in COUNTERS}
                if (any(v < 0 for v in delta.values())
                        or delta['input_tokens'] + delta['output_tokens'] != delta['total_tokens']
                        or delta['cached_input_tokens'] > delta['input_tokens']
                        or delta['reasoning_output_tokens'] > delta['output_tokens']):
                    continue
                result = {'turn_id': active[0], 'completed_at': event.get('timestamp'), **delta}
                active = None
    return result


def report(session, home):
    db = home / '.codex/state_5.sqlite'
    with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as conn:
        row = conn.execute('SELECT rollout_path FROM threads WHERE id=?', (session,)).fetchone()
    if not row:
        return None
    return completed_usage(row[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True)
    args = parser.parse_args()
    try:
        value = report(args.session, Path.home())
    except (OSError, ValueError, sqlite3.Error):
        value = None
    footer = (f"Last completed turn: {value['total_tokens']:,} tokens "
              f"({value['cached_input_tokens']:,} cached input included).") if value else UNKNOWN
    print(json.dumps({'status': 'recorded' if value else 'unavailable',
                      'usage': value, 'footer': footer}))


if __name__ == '__main__':
    main()
