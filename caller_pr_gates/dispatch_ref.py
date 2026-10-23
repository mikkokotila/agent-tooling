"""Create or reuse a commit-named dispatch tag; never move or delete a ref."""
import json
import subprocess


def tag_ref(repository, name, head, read, run, cwd):
    endpoint = f'repos/{repository}/git/matching-refs/tags/{name}'

    def existing():
        refs = read(endpoint)
        if not isinstance(refs, list):
            raise ValueError('Dispatch tag identity is unavailable')
        matches = [item for item in refs if item.get('ref') == 'refs/tags/' + name]
        if not matches:
            return False
        if len(matches) != 1 or matches[0].get('object') != {'sha': head, 'type': 'commit',
                'url': f'https://api.github.com/repos/{repository}/git/commits/{head}'}:
            raise ValueError('Existing dispatch tag differs from its named commit')
        return True

    if not existing():
        result = run(['gh', 'api', f'repos/{repository}/git/refs', '--method', 'POST', '--input', '-'],
                     runner=subprocess.run, cwd=cwd, input=json.dumps({'ref': 'refs/tags/' + name, 'sha': head}),
                     text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError('GitHub did not confirm dispatch tag creation; reconcile before retry')
        if not existing():
            raise RuntimeError('Created dispatch tag is unavailable; reconcile before retry')
    return name
