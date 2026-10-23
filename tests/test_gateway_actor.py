"""Read and write transports honor the same explicit GitHub account selection."""
import pytest
from caller_pr_gates import ghrelay


@pytest.mark.parametrize('user', [None, 'release-owner'])
def test_gateway_credentials_use_the_evidence_account(monkeypatch, user):
    if user is None: monkeypatch.delenv('GITHUB_INTERFACE_USER', raising=False)
    else: monkeypatch.setenv('GITHUB_INTERFACE_USER', user)
    calls = []
    def local(argv):
        calls.append(argv)
        return 'private-test-credential'
    monkeypatch.setattr(ghrelay, 'local', local)
    assert ghrelay.credentials() == 'private-test-credential'
    assert calls == [['auth', 'token', *(['--user', user] if user else [])]]
