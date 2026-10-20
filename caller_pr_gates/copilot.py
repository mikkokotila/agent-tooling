"""GitHub-managed review execution has no authority over source readiness.

API workflow provenance identifies service execution; authenticated bot identity,
review state and a complete known body identify service-only review notices.
Names alone never exempt CI or review feedback.
"""
import re

SERVICE_PATH = 'dynamic/agents/copilot-pull-request-reviewer'
SERVICE_CONTEXTS = frozenset({'copilot-pull-request-reviewer', 'Running Copilot Code Review'})

# Verified on public review 5578995082. GraphQL omits REST's [bot] suffix.
_REVIEWER_ID = 'BOT_kgDOCnlnWA'
_REVIEWER_LOGINS = ('copilot-pull-request-reviewer', 'copilot-pull-request-reviewer[bot]')
_QUOTA_NOTICE = 'Copilot was unable to review this pull request because the user who requested the review has reached their quota limit.'
_QUOTA_BODY = re.compile(re.escape(_QUOTA_NOTICE) + '(?: ' + re.escape(
    'An organization owner can choose the repository organization as the billing source for reviews requested by members with a paid Copilot license. '
    '[Learn about Copilot code review billing](https://docs.github.com/en/copilot/concepts/agents/code-review#code-review-usage). '
    '[Review the Copilot code review billing settings](')
    + r'https://github\.com/organizations/[A-Za-z0-9-]+/settings/copilot/policies\)\.)?')


def is_service_review(comment: dict) -> bool:
    """Only a complete, provenance-bound notice is unrelated to source review."""
    if (comment.get('kind') != 'review' or comment.get('review_state') != 'COMMENTED'
            or comment.get('author_type') != 'Bot' or comment.get('author_node_id') != _REVIEWER_ID
            or comment.get('author_login') not in _REVIEWER_LOGINS
            or not all(comment.get(key) for key in ('id', 'url', 'updated_at'))):
        return False
    body = comment.get('body')
    return isinstance(body, str) and bool(_QUOTA_BODY.fullmatch(' '.join(body.split())))


def service_identity(run: dict) -> dict:
    if (run.get('event') == 'dynamic' and run.get('path') == SERVICE_PATH
            and run.get('workflow_id')):
        return {'workflow_event': run['event'], 'workflow_path': run['path'],
                'workflow_id': str(run['workflow_id'])}
    return {}


def is_service_check(check: dict) -> bool:
    return (check.get('kind') in {'workflow', 'job', 'check'}
            and check.get('workflow_event') == 'dynamic'
            and check.get('workflow_path') == SERVICE_PATH
            and bool(check.get('workflow_id')))
