"""Add one native parent edge for a document owned by this chat."""
from __future__ import annotations

import subprocess

from .ghrelay import run as github_run

QUERY = '''query($owner:String!,$repo:String!,$parent:Int!,$child:Int!){
  repository(owner:$owner,name:$repo){
    parent:issue(number:$parent){databaseId number url}
    child:issue(number:$child){databaseId number url parent{number url}}
  }
}'''


async def link(store, sid, parent_url, child_url, *, client=None):
    from .cli import reference
    from .transport import GitHubClient

    repo, parent, parent_issue = reference(parent_url)
    child_repo, child, child_issue = reference(child_url)
    if not parent_issue or not child_issue or repo.lower() != child_repo.lower() or parent == child:
        raise ValueError('Link two distinct issues in the same repository')
    if not any(j['repository'].lower() == repo.lower() and j['number'] == child
               and j.get('issue_number') and j['kind'] in {'prd', 'slice'}
               for j in store.followups(sid)):
        raise ValueError('Child document is not owned by this chat')
    client = client or GitHubClient()
    owner, name = repo.split('/')
    variables = dict(owner=owner, repo=name, parent=parent, child=child)
    expected_parent = f'https://github.com/{repo}/issues/{parent}'
    expected_child = f'https://github.com/{repo}/issues/{child}'

    async def read():
        data = await client.graphql(QUERY, variables)
        nodes = data.get('repository') if isinstance(data, dict) else None
        if not isinstance(nodes, dict):
            raise ValueError('Native issue hierarchy evidence is unavailable')
        for key, number, url in [('parent', parent, expected_parent), ('child', child, expected_child)]:
            node = nodes.get(key)
            if (not isinstance(node, dict) or node.get('number') != number
                    or str(node.get('url') or '').lower() != url.lower()
                    or type(node.get('databaseId')) is not int or node['databaseId'] <= 0):
                raise ValueError('Native issue identity is unavailable or changed')
        if 'parent' not in nodes['child']:
            raise ValueError('Current parent evidence is unavailable')
        return nodes

    nodes = await read()
    current = nodes['child']['parent']
    if current is not None:
        if (not isinstance(current, dict) or current.get('number') != parent
                or str(current.get('url') or '').lower() != expected_parent.lower()):
            raise ValueError('Child already has a different parent; no replacement performed')
        return dict(parent=expected_parent, child=expected_child, status='already_linked')

    result = github_run(['gh', 'api', f'repos/{repo}/issues/{parent}/sub_issues', '--method', 'POST',
                         '-F', f'sub_issue_id={nodes["child"]["databaseId"]}', '-F', 'replace_parent=false'],
                        runner=subprocess.run, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'Native issue link failed; verify before retrying')
    nodes = await read()
    current = nodes['child']['parent']
    if (not isinstance(current, dict) or current.get('number') != parent
            or str(current.get('url') or '').lower() != expected_parent.lower()):
        raise ValueError('Native issue link is not verified; reread before retrying')
    store.audit('issue_hierarchy_linked', dict(session_id=sid, parent=expected_parent, child=expected_child))
    return dict(parent=expected_parent, child=expected_child, status='linked')
