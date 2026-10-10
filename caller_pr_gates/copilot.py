"""GitHub-managed review execution has no authority over source readiness.

Only API workflow provenance identifies the service. A repository check whose
name contains Copilot is still ordinary CI. Submitted review feedback is never
classified here and retains every review disposition/conclusion obligation.
"""
SERVICE_PATH = 'dynamic/agents/copilot-pull-request-reviewer'
SERVICE_CONTEXTS = frozenset({'copilot-pull-request-reviewer', 'Running Copilot Code Review'})


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
