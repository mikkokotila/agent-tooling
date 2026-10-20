"""Read complete, current-head GitHub evidence through Caller's authenticated client."""
from __future__ import annotations

import asyncio
import ast
import json
import re
import time
from typing import Any
from urllib.parse import quote


class EvidenceError(RuntimeError):
    """GitHub did not provide complete evidence; completion must fail closed."""


from .errors import diagnostic as _component_error


ISSUE_QUERY = """query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) { issue(number:$number) {
    lastEditedAt updatedAt body
  } }
}"""


SUMMARY_QUERY = """query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) { pullRequest(number:$number) {
    headRefOid mergeStateStatus reviewDecision baseRefName baseRef { branchProtectionRule { requiredStatusCheckContexts } }
  } }
}"""
THREAD_QUERY = """query($owner:String!, $repo:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$repo) { pullRequest(number:$number) {
    reviewThreads(first:100, after:$cursor) { nodes {
      id isResolved isOutdated comments(first:100) { nodes {
        id databaseId fullDatabaseId body url createdAt updatedAt author { login }
      } pageInfo { hasNextPage endCursor } }
    } pageInfo { hasNextPage endCursor } }
  } }
}"""
THREAD_COMMENTS_QUERY = """query($id:ID!, $cursor:String) {
  node(id:$id) { ... on PullRequestReviewThread {
    comments(first:100, after:$cursor) { nodes {
      id databaseId fullDatabaseId body url createdAt updatedAt author { login }
    } pageInfo { hasNextPage endCursor } }
  } }
}"""
REVIEWS_QUERY = """query($owner:String!, $repo:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$repo) { pullRequest(number:$number) {
    reviews(first:100, after:$cursor) { nodes {
      id databaseId fullDatabaseId body state url submittedAt createdAt updatedAt author { login __typename ... on Bot { id } }
    } pageInfo { hasNextPage endCursor } }
  } }
}"""
COMMITS_QUERY = """query($owner:String!, $repo:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$repo) { pullRequest(number:$number) {
    commits(first:100, after:$cursor) { nodes { commit { oid committedDate } }
      pageInfo { hasNextPage endCursor } }
  } }
}"""


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"Missing or malformed {label}")
    return value


def _pull(data: Any) -> dict[str, Any]:
    repository = _mapping(_mapping(data, "GraphQL response").get("repository"), "repository")
    return _mapping(repository.get("pullRequest"), "pull request")


def _page(connection: Any, label: str) -> tuple[list[dict[str, Any]], str | None]:
    connection = _mapping(connection, label)
    nodes = connection.get("nodes")
    info = _mapping(connection.get("pageInfo"), f"{label} page information")
    if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
        raise EvidenceError(f"Malformed {label} nodes")
    if not isinstance(info.get("hasNextPage"), bool):
        raise EvidenceError(f"Missing {label} pagination state")
    cursor = info.get("endCursor") if info["hasNextPage"] else None
    if info["hasNextPage"] and (not isinstance(cursor, str) or not cursor):
        raise EvidenceError(f"Missing next {label} cursor")
    return nodes, cursor


async def _rest_pages(client: Any, path: str, params: dict | None = None,
                      field: str | None = None) -> list[dict[str, Any]]:
    """Handle both REST arrays and object-wrapped check/action endpoints."""
    result: list[dict[str, Any]] = []
    page = 1
    total: int | None = None
    seen: set[str] = set()
    while True:
        data = await client.get(path, params={**(params or {}), "per_page": 100, "page": page})
        if field:
            data = _mapping(data, path)
            count = data.get("total_count")
            if count is not None:
                if not isinstance(count, int) or count < 0:
                    raise EvidenceError(f"Malformed total count for {path}")
                total = count if total is None else max(total, count)
                if "/actions/runs" in path and total > 1000:
                    raise EvidenceError("GitHub's filtered workflow API cannot enumerate more than 1000 runs")
            data = data.get(field)
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise EvidenceError(f"Malformed paginated response: {path}")
        for item in data:
            # PR files share a blob SHA when different paths contain identical
            # bytes. Their stable identity is the path, including across pages.
            if re.fullmatch(r'/repos/[^/]+/[^/]+/pulls/[0-9]+/files', path):
                identity = item.get('filename')
                if not isinstance(identity, str) or not identity:
                    raise EvidenceError(f'Missing file path for {path}')
            else:
                identity = str(item.get("id") or item.get("sha") or "")
            if identity and identity in seen:
                raise EvidenceError(f"Pagination changed while collecting {path}; retry required")
            seen.add(identity)
        result.extend(data)
        if len(data) < 100:
            if total is not None and len(result) < total:
                raise EvidenceError(f"Incomplete pagination for {path}: {len(result)}/{total}")
            return result
        page += 1
        if page > 10000:
            raise EvidenceError(f"Unbounded pagination for {path}")


async def _rules_evidence(client: Any, path: str) -> tuple[list[str] | None, dict | None]:
    """A rules API gap must not discard independent PR/CI/review evidence."""
    try:
        expected: list[str] = []
        for rule in await _rest_pages(client, path):
            if rule.get("type") == "required_status_checks":
                parameters = _mapping(rule.get("parameters"), "status-check rule")
                contexts = parameters.get("required_status_checks")
                if not isinstance(contexts, list):
                    raise EvidenceError("Missing ruleset required check contexts")
                for context in contexts:
                    context = _mapping(context, "required ruleset check").get("context")
                    if not isinstance(context, str) or not context:
                        raise EvidenceError("Malformed ruleset check context")
                    expected.append(context)
            elif rule.get("type") == "workflows":
                # A name alone cannot prove required workflows in other repositories.
                raise EvidenceError("Required-workflow rules need explicit workflow identity support")
        return expected, None
    except Exception as error:
        return None, _component_error(error, path)


async def _threads(client: Any, variables: dict) -> list[dict]:
    result = []
    cursor = None
    cursors = set()
    while True:
        data = _pull(await client.graphql(THREAD_QUERY, {**variables, "cursor": cursor}))
        batch, next_cursor = _page(data.get("reviewThreads"), "review threads")
        for thread in batch:
            if not isinstance(thread.get("isResolved"), bool) or not isinstance(thread.get("isOutdated"), bool):
                raise EvidenceError("Missing review thread resolution/outdated state")
            comments, comment_cursor = _page(thread.get("comments"), "thread comments")
            comment_cursors = set()
            while comment_cursor:
                if comment_cursor in comment_cursors:
                    raise EvidenceError("Repeated review-comment pagination cursor")
                comment_cursors.add(comment_cursor)
                node = _mapping(await client.graphql(THREAD_COMMENTS_QUERY,
                    {"id": thread["id"], "cursor": comment_cursor}), "thread response")
                node = _mapping(node.get("node"), "review thread")
                comment_batch, comment_cursor = _page(node.get("comments"), "thread comments")
                comments.extend(comment_batch)
            thread["comments"] = comments
            result.append(thread)
        if not next_cursor:
            return result
        if next_cursor in cursors:
            raise EvidenceError("Repeated review-thread pagination cursor")
        cursors.add(next_cursor)
        cursor = next_cursor


async def _commits(client: Any, variables: dict) -> list[dict]:
    result = []
    cursor = None
    cursors = set()
    while True:
        pull = _pull(await client.graphql(COMMITS_QUERY, {**variables, "cursor": cursor}))
        nodes, next_cursor = _page(pull.get("commits"), "pull request commits")
        for node in nodes:
            commit = _mapping(node.get("commit"), "commit")
            if not commit.get("oid") or not commit.get("committedDate"):
                raise EvidenceError("Missing commit identity/date")
            result.append(commit)
        if not next_cursor:
            return result
        if next_cursor in cursors:
            raise EvidenceError("Repeated commit pagination cursor")
        cursors.add(next_cursor)
        cursor = next_cursor


async def _reviews(client: Any, variables: dict) -> list[dict]:
    result = []
    cursor = None
    cursors = set()
    while True:
        pull = _pull(await client.graphql(REVIEWS_QUERY, {**variables, "cursor": cursor}))
        nodes, next_cursor = _page(pull.get("reviews"), "pull request reviews")
        result.extend(nodes)
        if not next_cursor:
            return result
        if next_cursor in cursors:
            raise EvidenceError("Repeated review pagination cursor")
        cursors.add(next_cursor)
        cursor = next_cursor


def _comment(raw: dict, kind: str, thread: dict | None = None) -> dict:
    graphql = kind in {"inline", "review"}
    identity = (raw.get("fullDatabaseId") or raw.get("databaseId")) if graphql else raw.get("id")
    author = raw.get("author") if graphql else raw.get("user")
    author = author or {}
    updated = raw.get("updatedAt") if graphql else (raw.get("updated_at") or raw.get("submitted_at"))
    created = raw.get("createdAt") if graphql else (raw.get("created_at") or raw.get("submitted_at"))
    body = raw.get("body")
    url = raw.get("url") if graphql else raw.get("html_url")
    if identity is None or not isinstance(body, str) or not updated or not url:
        raise EvidenceError(f"Incomplete {kind} comment evidence")
    # A deleted reviewer still leaves an obligation; its author may be null.
    return {"id": f"{kind}:{identity}", "database_id": str(identity), "node_id": raw.get("id") if graphql else raw.get("node_id"),
            "thread_id": thread["id"] if thread else None,
            "author_login": str(author.get("login") or "[deleted]"), "body": body,
            "author_type": author.get("__typename") if graphql else author.get("type"),
            "author_node_id": author.get("id") if graphql else author.get("node_id"),
            "review_state": raw.get("state") if kind == "review" else None,
            "created_at": created, "updated_at": updated, "url": url,
            "is_resolved": bool(thread and thread["isResolved"]),
            "is_outdated": bool(thread and thread["isOutdated"]), "kind": kind, "replies": []}


def _check(raw: dict, kind: str, *, key: str | None = None, attempt: int = 1) -> dict:
    status = raw.get("state") if kind == "status" else raw.get("status")
    name = raw.get("context") if kind == "status" else raw.get("name")
    conclusion = status if kind == "status" else raw.get("conclusion")
    if raw.get("id") is None or not name or not status:
        raise EvidenceError(f"Incomplete individual {kind} evidence")
    if kind == "status":
        status = "completed" if status in {"success", "failure", "error"} else status
    url = raw.get("target_url") if kind == "status" else (raw.get("html_url") or raw.get("details_url"))
    return {"id": str(raw["id"]), "name": name, "kind": kind, "key": key or f"{kind}:{name}",
            "status": status, "conclusion": conclusion, "url": url,
            "updated_at": raw.get("updated_at") or raw.get("completed_at") or raw.get("created_at") or raw.get("run_started_at"),
            "created_at": raw.get("created_at") or raw.get("started_at"),
            "run_attempt": attempt, "head_sha": raw.get("head_sha")}


async def collect_pr(repository: str, number: int, token_user: str | None = None) -> dict:
    from .transport import GitHubClient
    client=GitHubClient(token_user)
    from .collections import Collections
    return await Collections(client.coordinator,client.account).run('full-pr',f'{repository}/pull/{number}',
        lambda: collect_with_client(_BatchClient(client),repository,number))


async def _branch_evidence(client, prefix, base_ref, head):
    receipt = {'base_ref': base_ref}
    path = f'{prefix}/branches/{quote(base_ref, safe="")}'
    try:
        branch = _mapping(await client.get(path), 'current target branch')
        base = _mapping(branch.get('commit'), 'target branch commit').get('sha')
        if not isinstance(base, str) or not re.fullmatch(r'[a-fA-F0-9]{40}', base):
            raise EvidenceError('Target branch has no full commit SHA')
        receipt['base_sha'] = base
        path = f'{prefix}/compare/{base}...{head}'
        compared = _mapping(await client.get(path, params={'per_page': 1}), 'branch ancestry comparison')
        if _mapping(compared.get('base_commit'), 'comparison base').get('sha') != base:
            raise EvidenceError('Branch comparison returned a different base commit')
        receipt['branch_comparison'] = {'base_sha': base, 'head_sha': head,
            'merge_base_sha': _mapping(compared.get('merge_base_commit'), 'comparison ancestor').get('sha'),
            'ahead_by': compared.get('ahead_by'), 'behind_by': compared.get('behind_by'),
            'status': compared.get('status')}
    except Exception as error:
        receipt['branch_error'] = _component_error(error, path)
    return receipt


async def _branch_final_read(client, prefix, receipt):
    path = f'{prefix}/branches/{quote(receipt["base_ref"], safe="")}'
    try:
        branch = _mapping(await client.get(path), 'final target branch read')
        receipt['base_sha_after'] = _mapping(branch.get('commit'), 'final target branch commit').get('sha')
    except Exception as error:
        receipt['branch_error'] = _component_error(error, path)
    return receipt


async def collect_with_client(client: Any, repository: str, number: int) -> dict:
    """Injectable collector for offline tests; never performs GitHub mutations."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or number < 1:
        raise ValueError("Expected owner/repository and positive PR number")
    owner, repo = repository.split("/")
    prefix = f"/repos/{repository}"
    path = f"{prefix}/pulls/{number}"
    raw, actor, summary = await asyncio.gather(client.get(path), client.get("/user"),
        client.graphql(SUMMARY_QUERY, {"owner": owner, "repo": repo, "number": number}))
    raw = _mapping(raw, "REST pull request")
    summary = _pull(summary)
    head = _mapping(raw.get("head"), "pull request head").get("sha")
    if not head or not summary.get("headRefOid"):
        raise EvidenceError("Missing current head")
    if summary["headRefOid"] != head:
        raise EvidenceError("PR head changed before evidence collection; retry required")
    if not isinstance(raw.get("draft"), bool) or not isinstance(raw.get("merged"), bool):
        raise EvidenceError("Missing draft/merged state")
    actor_login = _mapping(actor, "authenticated user").get("login")
    author = _mapping(raw.get("user"), "PR author").get("login")
    if not actor_login or not author or raw.get("state") not in {"open", "closed"}:
        raise EvidenceError("Missing PR lifecycle/author or authenticated actor")
    variables = {"owner": owner, "repo": repo, "number": number}
    base_ref = _mapping(summary.get("baseRef"), "base branch reference")
    protection = base_ref.get("branchProtectionRule")
    expected: list[str] = []
    if protection is not None:
        contexts = _mapping(protection, "branch protection").get("requiredStatusCheckContexts")
        if not isinstance(contexts, list) or any(not isinstance(x, str) for x in contexts):
            raise EvidenceError("Missing required branch-protection check contexts")
        expected.extend(contexts)
    base_name = summary.get("baseRefName")
    if not isinstance(base_name, str) or not base_name:
        raise EvidenceError("Missing base branch")
    from .closure import collect as collect_closure
    initial_branch = await _branch_evidence(client, prefix, base_name, head)
    immutable_key=None;cached_files=None
    if isinstance(client,_BatchClient) and not initial_branch.get('branch_error'):
        immutable_key=json.dumps(['files',repository,number,head,base_name,initial_branch.get('base_sha'),raw.get('changed_files')])
        cached_files=client.collections.immutable(immutable_key)
    async def files_read():
        return cached_files if cached_files is not None else await _rest_pages(client,f'{path}/files')
    async def branch_read():return initial_branch
    threads, reviews, issues, commits, files, checkruns, statuses, workflows, rule_evidence, branch_evidence, closure_evidence = await asyncio.gather(
        _threads(client, variables), _reviews(client, variables),
        _rest_pages(client, f"{prefix}/issues/{number}/comments"), _commits(client, variables),
        files_read(),
        _rest_pages(client, f"{prefix}/commits/{head}/check-runs", {"filter": "all"}, "check_runs"),
        _rest_pages(client, f"{prefix}/commits/{head}/statuses"),
        _rest_pages(client, f"{prefix}/actions/runs", {"head_sha": head}, "workflow_runs"),
        _rules_evidence(client, f"{prefix}/rules/branches/{quote(base_name, safe='')}"),
        branch_read(), collect_closure(client, repository, number))
    if not isinstance(raw.get("changed_files"), int) or len(files) != raw["changed_files"]:
        raise EvidenceError("Changed-file enumeration is incomplete (GitHub caps it at 3000)")
    if not isinstance(raw.get("commits"), int) or len(commits) != raw["commits"]:
        raise EvidenceError("Commit enumeration changed or is incomplete")
    rule_contexts, rule_error = rule_evidence
    if rule_contexts is not None:
        expected.extend(rule_contexts)
    # GitHub answers the rules API with "upgrade to GitHub Pro or make this
    # repository public" when the repository's plan cannot have rulesets at all.
    # No ruleset can then require a check, so required-check discovery is
    # complete from branch protection alone; every actual check is still judged.
    # The answer stays in component_errors as evidence.
    rules_absent_on_plan = bool(rule_error) and rule_error.get("kind") == "feature_unavailable"
    comments: list[dict] = []
    for thread in threads:
        normalized = [_comment(item, "inline", thread) for item in thread["comments"]]
        for index, comment in enumerate(normalized):
            comment["replies"] = [dict(reply) for reply in normalized[index + 1:]]
        comments.extend(normalized)
    for review in reviews:
        if review.get("state") == "PENDING":
            continue
        if review.get("body", "").strip() or review.get("state") == "CHANGES_REQUESTED":
            review = dict(review)
            review["body"] = review.get("body") or "Review requests changes."
            comments.append(_comment(review, "review"))
    issue_comments = [_comment(comment, "issue") for comment in issues]
    comments.extend(issue_comments)
    for comment in comments:
        if comment["kind"] != "inline":
            comment["replies"] = [dict(reply) for reply in issue_comments
                if reply["id"] != comment["id"] and str(reply["created_at"] or "") > str(comment["updated_at"])]
    selected_runs: dict[str, dict] = {}
    for run in workflows:
        if run.get("head_sha") != head:
            raise EvidenceError("Workflow API returned evidence for a different head")
        key = f"workflow:{run.get('workflow_id')}:{run.get('event')}"
        if not run.get("workflow_id") or not run.get("event"):
            raise EvidenceError("Missing workflow identity")
        prior = selected_runs.get(key)
        if prior is None or (str(run.get("created_at") or ""), int(run["id"])) > (str(prior.get("created_at") or ""), int(prior["id"])):
            selected_runs[key] = run
    jobs_by_run = await asyncio.gather(*[
        _rest_pages(client, f"{prefix}/actions/runs/{run['id']}/attempts/{run.get('run_attempt', 1)}/jobs", field="jobs")
        for run in selected_runs.values()])
    checks: list[dict] = []
    attempts_by_check: dict[str, int] = {}
    services_by_check: dict[str, dict] = {}
    from .copilot import service_identity
    for (key, run), jobs in zip(selected_runs.items(), jobs_by_run):
        attempt = int(run.get("run_attempt") or 1)
        service = service_identity(run)
        checks.append(dict(_check(run, "workflow", key=key, attempt=attempt), **service))
        for job in jobs:
            if job.get("head_sha") and job["head_sha"] != head:
                raise EvidenceError("Workflow job belongs to a different head")
            checks.append(dict(_check(job, "job", key=f"job:{key}:{job.get('name')}", attempt=attempt), **service))
            match = re.search(r"/check-runs/(\d+)$", str(job.get("check_run_url") or ""))
            if match:
                attempts_by_check[match[1]] = attempt
                if service:
                    services_by_check[match[1]] = service
    for item in checkruns:
        if item.get("head_sha") != head:
            raise EvidenceError("Check run belongs to a different head")
        app = _mapping(item.get("app"), "check-run application")
        if not app.get("id"):
            raise EvidenceError("Missing check-run application identity")
        service = services_by_check.get(str(item['id']), {}) if app.get('id') == 15368 and app.get('slug') == 'github-actions' else {}
        checks.append(dict(_check(item, "check", key=f"check:{app.get('id')}:{item.get('name')}",
                             attempt=attempts_by_check.get(str(item["id"]), 1)), **service))
    checks.extend(_check(item, "status") for item in statuses)
    after = _mapping(await client.get(path), "final pull request read")
    after_head = _mapping(after.get("head"), "final pull request head").get("sha")
    if not after_head:
        raise EvidenceError("Missing final head")
    # Metadata changes also invalidate an apparently clean read, even on a stable commit.
    for field in ("state", "merged", "draft", "updated_at"):
        if raw.get(field) != after.get(field):
            raise EvidenceError(f"PR {field} changed during evidence collection; retry required")
    branch_evidence = await _branch_final_read(client, prefix, branch_evidence)
    branch_error = branch_evidence.pop('branch_error', None)
    branch_evidence['base_ref_after'] = (after.get('base') or {}).get('ref')
    closure_error = closure_evidence.pop('closure_error', None)
    if immutable_key and not branch_error and head==after_head and branch_evidence.get('base_sha')==branch_evidence.get('base_sha_after') and base_name==branch_evidence.get('base_ref_after'):
        client.collections.remember(immutable_key,files)
    return {"repository": repository, "number": number, "url": raw.get("html_url"),
        "state": raw["state"], "merged": raw["merged"], "draft": raw["draft"],
        "mergeable": after.get('mergeable'),
        "merge_state_status": summary.get('mergeStateStatus'),
        **({'review_decision': summary['reviewDecision']} if 'reviewDecision' in summary else {}),
        "merge_readiness_head_sha": summary['headRefOid'],
        "head_sha": head, "head_sha_after": after_head, "head_ref": (raw.get("head") or {}).get("ref"), "head_repository": ((raw.get("head") or {}).get("repo") or {}).get("full_name"), "author_login": author,
        "actor_login": actor_login, "collected_at": time.time(), "checks": checks,
        "expected_checks": sorted(set(expected)) if rule_error is None or rules_absent_on_plan else None,
        "known_expected_checks": sorted(set(expected)),
        **branch_evidence,
        **closure_evidence,
        "component_errors": {**({'rules': rule_error} if rule_error else {}),
                             **({'branch_freshness': branch_error} if branch_error else {}),
                             **({'issue_closure': closure_error} if closure_error else {})},
        "component_status": {"rules": "not_available_on_plan" if rules_absent_on_plan
                             else "unavailable" if rule_error else "complete"},
        "comments": comments,
        "commit_shas": [commit["oid"] for commit in commits],
        "commit_dates": {commit["oid"]: commit["committedDate"] for commit in commits},
        "changed_files": [item["filename"] for item in files], "title": raw.get("title"),
        "body": raw.get("body") or "", "created_at": raw.get("created_at")}


async def collect_issue(repository: str, number: int, token_user: str | None = None) -> dict:
    from .transport import GitHubClient
    client=GitHubClient(token_user)
    from .collections import Collections
    return await Collections(client.coordinator,client.account).run('issue',f'{repository}/issues/{number}',
        lambda: collect_issue_with_client(client,repository,number))


async def collect_issue_with_client(client: Any, repository: str, number: int) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or number < 1:
        raise ValueError("Expected owner/repository and positive issue number")
    path = f"/repos/{repository}/issues/{number}"
    owner, repo = repository.split("/")
    raw, actor, metadata = await asyncio.gather(client.get(path), client.get("/user"),
        client.graphql(ISSUE_QUERY, {"owner": owner, "repo": repo, "number": number}))
    metadata = _mapping(_mapping(_mapping(metadata, "issue metadata").get("repository"), "issue repository").get("issue"), "issue metadata")
    if metadata.get("body") != (raw.get("body") or ""):
        raise EvidenceError("Issue body changed before evidence collection; retry required")
    raw, actor = _mapping(raw, "issue"), _mapping(actor, "authenticated user")
    if raw.get("pull_request"):
        raise EvidenceError("Use PR collection for pull requests")
    author = _mapping(raw.get("user"), "issue author").get("login")
    if not actor.get("login") or not author or not raw.get("updated_at"):
        raise EvidenceError("Missing issue author/actor/revision")
    comments = [_comment(item, "issue") for item in await _rest_pages(client, f"{path}/comments")]
    for comment in comments:
        comment["replies"] = [dict(reply) for reply in comments
            if reply["id"] != comment["id"] and str(reply["created_at"] or "") > str(comment["updated_at"])]
    after = _mapping(await client.get(path), "final issue read")
    if any(raw.get(field) != after.get(field) for field in ("updated_at", "state", "title", "body")):
        raise EvidenceError("Issue changed during evidence collection; retry required")
    return {"repository": repository, "number": number, "url": raw.get("html_url"),
            "kind": "issue", "state": raw.get("state"), "author_login": author,
            "actor_login": actor["login"], "collected_at": time.time(), "comments": comments,
            "title": raw.get("title") or "", "body": raw.get("body") or "",
            "created_at": raw.get("created_at"), "updated_at": raw["updated_at"],
            "document_edited_at": metadata.get("lastEditedAt"), "commit_shas": [], "changed_files": []}

# These first pages share one authenticated GraphQL response. Each existing
# enumerator still validates and fetches all additional pages independently.
BATCH_QUERY = '''query($owner:String!, $repo:String!, $number:Int!) {
 viewer { login }
 repository(owner:$owner,name:$repo) { defaultBranchRef { name }
  pullRequest(number:$number) {
   headRefOid mergeStateStatus reviewDecision baseRefName state merged baseRef { branchProtectionRule { requiredStatusCheckContexts } }
   closingIssuesReferences(first:100) { nodes { number url state repository { nameWithOwner } } pageInfo { hasNextPage endCursor } }
   reviewThreads(first:100) { nodes { id isResolved isOutdated comments(first:100) { nodes {
    id databaseId fullDatabaseId body url createdAt updatedAt author { login }
   } pageInfo { hasNextPage endCursor } } } pageInfo { hasNextPage endCursor } }
   reviews(first:100) { nodes { id databaseId fullDatabaseId body state url submittedAt createdAt updatedAt author { login __typename ... on Bot { id } } } pageInfo { hasNextPage endCursor } }
   commits(first:100) { nodes { commit { oid committedDate } } pageInfo { hasNextPage endCursor } }
  }
 }
}'''

class _BatchClient:
    def __init__(self,client):
        from .collections import Collections
        self.client=client;self.seed_task=None;self.variables=None
        self.collections=Collections(client.coordinator,client.account)

    async def seed(self,variables=None):
        if variables:self.variables={k:v for k,v in variables.items() if k in {'owner','repo','number'}}
        if not self.seed_task:
            # /user and SUMMARY run together; only SUMMARY supplies variables.
            while self.variables is None:await asyncio.sleep(0)
            if not self.seed_task:self.seed_task=asyncio.create_task(self.client.graphql(BATCH_QUERY,self.variables))
        return await self.seed_task

    async def get(self,path,**kwargs):
        if path=='/user':return _mapping((await self.seed()).get('viewer'),'authenticated viewer')
        return await self.client.get(path,**kwargs)

    async def graphql(self,query,variables):
        from .closure import QUERY
        if query==SUMMARY_QUERY or query in {THREAD_QUERY,REVIEWS_QUERY,COMMITS_QUERY,QUERY} and variables.get('cursor') is None:
            return json.loads(json.dumps(await self.seed(variables)))
        return await self.client.graphql(query,variables)


async def collect_validation_with_client(client,repository,number):
    """Only fresh lifecycle/head/target ancestry. Cannot satisfy completion."""
    prefix=f'/repos/{repository}';path=f'{prefix}/pulls/{number}'
    raw=_mapping(await client.get(path),'validation PR')
    head=_mapping(raw.get('head'),'PR head').get('sha')
    base=_mapping(raw.get('base'),'PR base').get('ref')
    if not re.fullmatch(r'[a-fA-F0-9]{40}',str(head)) or not base or type(raw.get('merged')) is not bool or raw.get('state') not in {'open','closed'}:
        raise EvidenceError('Missing validation lifecycle/head/target evidence')
    receipt=await _branch_evidence(client,prefix,base,head)
    after=_mapping(await client.get(path),'final validation PR')
    if any(raw.get(k)!=after.get(k) for k in ('state','merged','draft')):
        raise EvidenceError('PR lifecycle changed during validation preflight')
    if (after.get('base') or {}).get('ref')!=base:raise EvidenceError('PR target changed during validation preflight')
    if (after.get('head') or {}).get('sha')!=head:raise EvidenceError('PR head changed during validation preflight')
    # Retained qualification needs the merged original head, never a caller's
    # replacement commit. These fields do not change open-PR ancestry gates.
    merged_source = {
        'merge_commit_sha': raw.get('merge_commit_sha'),
        'head_repository': ((raw.get('head') or {}).get('repo') or {}).get('full_name'),
        'base_repository': ((raw.get('base') or {}).get('repo') or {}).get('full_name'),
    }
    merged_source_after = {
        'merge_commit_sha': after.get('merge_commit_sha'),
        'head_repository': ((after.get('head') or {}).get('repo') or {}).get('full_name'),
        'base_repository': ((after.get('base') or {}).get('repo') or {}).get('full_name'),
    }
    if raw['merged'] and merged_source != merged_source_after:
        raise EvidenceError('Merged source identity changed during validation preflight')
    receipt=await _branch_final_read(client,prefix,receipt)
    error=receipt.pop('branch_error',None)
    return dict(repository=repository,number=number,profile='validation',state=raw['state'],merged=raw['merged'],
        head_sha=head,head_sha_after=(after.get('head') or {}).get('sha'),base_ref_after=(after.get('base') or {}).get('ref'),
        collected_at=time.time(),component_errors={'branch_freshness':error} if error else {},**merged_source,**receipt)


async def collect_validation(repository,number,token_user=None):
    from .transport import GitHubClient
    from .collections import Collections
    client=GitHubClient(token_user)
    return await Collections(client.coordinator,client.account).run('validation',f'{repository}/pull/{number}',
        lambda:collect_validation_with_client(client,repository,number))


async def collect_review_with_client(client,repository,number):
    """Current actors, complete review/reply inventory and commit chronology."""
    owner,repo=repository.split('/');variables={'owner':owner,'repo':repo,'number':number}
    path=f'/repos/{repository}/pulls/{number}'
    raw,actor=await asyncio.gather(client.get(path),client.get('/user'))
    raw=_mapping(raw,'review PR');head=_mapping(raw.get('head'),'review head').get('sha')
    if not re.fullmatch(r'[a-fA-F0-9]{40}',str(head)):raise EvidenceError('Missing current review head')
    threads,reviews,issues,commits=await asyncio.gather(_threads(client,variables),_reviews(client,variables),
        _rest_pages(client,f'/repos/{repository}/issues/{number}/comments'),_commits(client,variables))
    if len(commits)!=raw.get('commits'):raise EvidenceError('Review commit chronology incomplete')
    comments=[]
    for thread in threads:
        batch=[_comment(c,'inline',thread) for c in thread['comments']]
        for i,c in enumerate(batch):c['replies']=[dict(r) for r in batch[i+1:]]
        comments.extend(batch)
    for review in reviews:
        if review.get('state')!='PENDING' and (str(review.get('body') or '').strip() or review.get('state')=='CHANGES_REQUESTED'):
            comments.append(_comment({**review,'body':review.get('body') or 'Review requests changes.'},'review'))
    issue_comments=[_comment(c,'issue') for c in issues];comments.extend(issue_comments)
    for c in comments:
        if c['kind']!='inline':c['replies']=[dict(r) for r in issue_comments if r['id']!=c['id'] and str(r.get('created_at') or '')>str(c['updated_at'])]
    after=_mapping(await client.get(path),'final review PR')
    if any(raw.get(k)!=after.get(k) for k in ('state','merged','updated_at','base')) or (after.get('head') or {}).get('sha')!=head:
        raise EvidenceError('PR changed during review collection')
    actor_login=_mapping(actor,'authenticated actor').get('login');author=_mapping(raw.get('user'),'PR author').get('login')
    if not actor_login or not author or type(raw.get('merged')) is not bool or raw.get('state') not in {'open','closed'}:raise EvidenceError('Incomplete review actors/lifecycle')
    return dict(repository=repository,number=number,profile='review',state=raw['state'],merged=raw['merged'],
        head_sha=head,head_sha_after=head,actor_login=actor_login,author_login=author,comments=comments,
        commit_shas=[c['oid'] for c in commits],commit_dates={c['oid']:c['committedDate'] for c in commits},collected_at=time.time())


async def collect_review(repository,number,token_user=None):
    from .transport import GitHubClient
    from .collections import Collections
    client=GitHubClient(token_user)
    return await Collections(client.coordinator,client.account).run('review',f'{repository}/pull/{number}',
        lambda:collect_review_with_client(client,repository,number))
