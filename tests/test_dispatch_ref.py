import json
import subprocess

import pytest

from caller_pr_gates.dispatch_ref import tag_ref


def test_dispatch_creates_only_the_commit_named_tag_and_never_moves_it():
    refs, writes = [], []
    head, repo, name = 'a' * 40, 'Vaquum/Limen', 'v5.46.2-dispatch-' + 'a' * 40

    def read(path):
        assert path == f'repos/{repo}/git/matching-refs/tags/{name}'
        return refs

    def run(argv, **kwargs):
        assert argv == ['gh', 'api', f'repos/{repo}/git/refs', '--method', 'POST', '--input', '-']
        writes.append(json.loads(kwargs['input']))
        refs.append(dict(ref='refs/tags/' + name, object=dict(sha=head, type='commit',
            url=f'https://api.github.com/repos/{repo}/git/commits/{head}')))
        return subprocess.CompletedProcess(argv, 0, stdout='{}')

    assert tag_ref(repo, name, head, read, run, '.') == name
    assert writes == [dict(ref='refs/tags/' + name, sha=head)]
    assert tag_ref(repo, name, head, read, run, '.') == name
    assert len(writes) == 1
    refs[0]['object']['sha'] = 'b' * 40
    with pytest.raises(ValueError, match='differs'):
        tag_ref(repo, name, head, read, run, '.')
    assert len(writes) == 1


@pytest.mark.parametrize('fault', ['unknown', 'creation', 'readback'])
def test_dispatch_ref_refuses_unknown_or_unconfirmed_identity(fault):
    writes = []

    def read(_path):
        return {} if fault == 'unknown' else []

    def run(argv, **kwargs):
        writes.append(argv)
        return subprocess.CompletedProcess(argv, int(fault == 'creation'), stdout='{}')

    with pytest.raises((ValueError, RuntimeError)):
        tag_ref('Vaquum/Limen', 'v5.46.2-dispatch-' + 'a' * 40, 'a' * 40, read, run, '.')
    assert len(writes) == (0 if fault == 'unknown' else 1)
