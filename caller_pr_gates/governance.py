"""Admit only the approved Poise/Origo protection changes, preserving every other rule."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess


FIELDS = ('name', 'target', 'enforcement', 'conditions', 'bypass_actors', 'rules')


def _checks(checks, repository):
    if not isinstance(checks, list):
        raise ValueError('Governance protection check inventory is unavailable')
    result = []
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('context'), str):
            raise ValueError('Governance protection check identity is unavailable')
        check = dict(check)
        if repository == 'Vaquum/Origo':
            if check['context'] == 'pr_checks_version':
                continue
            if check['context'] == 'pr_checks_slice':
                check['context'] = 'pr_checks_prd'
        result.append(check)
    return result


def desired(before, repository, ruleset):
    if ruleset:
        result = {key: deepcopy(before[key]) for key in FIELDS}
        if result['target'] != 'branch' or result['enforcement'] != 'active':
            raise ValueError('Governance protection must remain an active branch ruleset')
        checks = [rule for rule in result['rules'] if rule.get('type') == 'required_status_checks']
        if len(checks) != 1:
            raise ValueError('Governance protection needs exactly one status-check rule')
        parameters = checks[0]['parameters']
        parameters['strict_required_status_checks_policy'] = False
        parameters['required_status_checks'] = _checks(parameters['required_status_checks'], repository)
        return result
    return {'strict': False, 'checks': _checks(before['checks'], repository)}


def configure(args, store, sid, repo, number, issue, endpoint, method, argv, run):
    repositories = {'autonomio/poise', 'Vaquum/Origo'}
    if repo not in repositories:
        return None
    prefix = f'repos/{repo}'
    classic = prefix + '/branches/main/protection/required_status_checks'
    ruleset = re.fullmatch(re.escape(prefix) + r'/rulesets/[1-9][0-9]*', endpoint) is not None
    label = repo == 'Vaquum/Origo' and endpoint == prefix + '/labels'
    if endpoint != classic and not ruleset and not label:
        return None
    expected_method = 'POST' if label else 'PUT' if ruleset else 'PATCH'
    if (issue or len(argv) != 6 or argv != ['api', endpoint, '--method', expected_method, '--input', argv[-1]]
            or method != expected_method or argv[-1] == '-'):
        raise ValueError('Governance configuration requires the exact owned PR, method and saved JSON file')
    path = Path(argv[-1])
    if not path.is_absolute():
        path = Path(args.cwd) / path
    payload = json.loads(path.read_text())
    scope = store.pr_scope(sid, repo, number) or {}
    if (not scope.get('approval_evidence') or repo.split('/')[1].casefold() not in str(scope.get('agreed_scope', '')).casefold()):
        raise ValueError('Governance configuration requires recorded human authorization for this repository')

    def owner():
        from .workflow import owned_pr
        session = store.session(sid)
        with store._connect() as db:
            watch = db.execute("SELECT * FROM pr_watch WHERE repository=? AND number=? AND owner_session=? AND state IN ('open','merged')",
                               (repo, number, sid)).fetchone()
        if (not session or session['root_session'] != sid or session['client'] != args.client
                or not watch or watch['client'] != args.client or Path(watch['cwd']).resolve() != Path(args.cwd).resolve()):
            raise ValueError('Governance configuration requires the recorded owner, client and checkout')
        if watch['state'] == 'merged':
            retained = store.closed_review_owner(sid, repo, number, args.cwd)
            if retained['identity']['watch']['state'] != 'merged':
                raise ValueError('Governance configuration requires a genuinely merged retained source')
        else:
            owned_pr(store, sid, repo, number)
        origin = subprocess.run(['git', '-C', args.cwd, 'remote', 'get-url', 'origin'],
                                capture_output=True, text=True, timeout=3, check=True).stdout.strip().removesuffix('.git')
        if origin.casefold() not in {f'https://github.com/{repo}'.casefold(), f'git@github.com:{repo}'.casefold(),
                                    f'ssh://git@github.com/{repo}'.casefold()}:
            raise ValueError('Governance configuration origin differs from the authorized repository')

    def read(target):
        result = run(['gh', 'api', target], runner=subprocess.run, cwd=args.cwd,
                     capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('Governance configuration read unavailable: ' + target)
        return json.loads(result.stdout)

    def pr_identity():
        pr = read(f'{prefix}/pulls/{number}')
        lifecycle = (pr.get('state'), pr.get('merged'))
        if (pr.get('number') != number or lifecycle not in {('open', False), ('closed', True)}
                or pr.get('draft') is not False or (pr.get('base') or {}).get('ref') != 'main'):
            raise ValueError('Governance configuration needs the recorded open or merged non-draft PR targeting main')
        identity = {'lifecycle': lifecycle}
        for side in ('base', 'head'):
            branch = pr.get(side) or {}
            if ((branch.get('repo') or {}).get('full_name') != repo
                    or not re.fullmatch(r'[0-9a-f]{40}', str(branch.get('sha')))):
                raise ValueError('Governance configuration PR repository/head identity is unavailable')
            identity[side] = (branch['sha'], branch.get('ref'))
        if lifecycle == ('closed', True):
            cached = (store.snapshot(repo, number) or {}).get('snapshot') or {}
            if cached.get('head_sha') != identity['head'][0]:
                raise ValueError('Merged governance continuation must retain its recorded source head')
        return identity

    owner()
    identity = pr_identity()
    if label:
        expected = dict(name='prd', color='5319e7', description='Product requirements document')
        if payload != expected:
            raise ValueError('Governance label creation admits only the required Origo prd label')
        prior = run(['gh', 'api', prefix + '/labels/prd'], runner=subprocess.run,
                    cwd=args.cwd, text=True, capture_output=True, timeout=30)
        if prior.returncode == 0:
            raise ValueError('The Origo prd label already exists; no write attempted')
        if '404' not in prior.stderr or 'not found' not in prior.stderr.casefold():
            raise ValueError('Origo prd label absence is unavailable; no write attempted')
        if pr_identity() != identity:
            raise ValueError('Governance label PR changed during admission')
        frozen = json.dumps(expected, sort_keys=True, separators=(',', ':'))
        receipt = dict(repository=repo, number=number, session_id=sid, endpoint=endpoint,
                       payload_sha256=hashlib.sha256(frozen.encode()).hexdigest())
        owner()
        store.audit('github_governance_configuration_attempt', receipt)
        result = run(['gh', 'api', endpoint, '--method', 'POST', '--input', '-'], runner=subprocess.run,
                     cwd=args.cwd, input=frozen, text=True, capture_output=True, timeout=90)
        if result.returncode:
            raise RuntimeError('Origo prd label creation outcome is incomplete; read back before retrying')
        actual = read(prefix + '/labels/prd')
        if any(actual.get(key) != value for key, value in expected.items()) or pr_identity() != identity:
            raise RuntimeError('Origo prd label readback differs; no automatic retry')
        owner()
        store.audit('github_governance_configuration_verified', receipt)
        return receipt
    before = read(endpoint)
    expected = desired(before, repo, ruleset)
    if payload != expected:
        raise ValueError('Governance configuration may only disable strict freshness and retire Origo slice/version checks')
    if ruleset and (before.get('source') != repo or before.get('source_type') != 'Repository'
                    or before.get('conditions', {}).get('ref_name', {}).get('include') not in [['refs/heads/main'], ['~DEFAULT_BRANCH']]):
        raise ValueError('Governance configuration ruleset must belong to this repository main branch')
    frozen = json.dumps(expected, sort_keys=True, separators=(',', ':'), allow_nan=False)
    receipt = dict(repository=repo, number=number, session_id=sid, endpoint=endpoint,
                   payload_sha256=hashlib.sha256(frozen.encode()).hexdigest())
    owner()
    if pr_identity() != identity or read(endpoint) != before:
        raise ValueError('Governance configuration changed during admission; no write attempted')
    store.audit('github_governance_configuration_attempt', receipt)
    result = run(['gh', 'api', endpoint, '--method', expected_method, '--input', '-'], runner=subprocess.run,
                 cwd=args.cwd, input=frozen, text=True, capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError('Governance configuration outcome is incomplete; read back before retrying')
    after = read(endpoint)
    actual = {key: after[key] for key in FIELDS} if ruleset else {'strict': after.get('strict'), 'checks': after.get('checks')}
    if actual != expected or pr_identity() != identity:
        raise RuntimeError('Governance configuration readback differs; no automatic retry or rollback')
    owner()
    store.audit('github_governance_configuration_verified', receipt)
    return receipt
