"""Local durable task/PR registry. SQLite transactions are the ownership boundary."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import time
from typing import Any
from urllib.parse import urlparse
import uuid

_STATUSES = frozenset({"pending", "running", "complete", "failed"})
_SCOPE_FIELDS = {"agreed_scope", "acceptance_criteria", "slice_count", "slice_ids", "approved_at"}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _epoch(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a nonnegative finite epoch timestamp")
    return float(value)


def _scope(value: dict) -> dict:
    if not isinstance(value, dict) or not _SCOPE_FIELDS.issubset(value):
        raise ValueError("scope requires agreed_scope, acceptance_criteria, slice_count, slice_ids, approved_at")
    if not isinstance(value["agreed_scope"], str) or not value["agreed_scope"].strip():
        raise ValueError("agreed_scope must be nonempty text")
    for name in ("acceptance_criteria", "slice_ids"):
        if not isinstance(value[name], list) or any(not isinstance(v, str) or not v.strip() for v in value[name]):
            raise ValueError(f"{name} must be a list of nonempty strings")
    count = value["slice_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("slice_count must be a nonnegative integer")
    if len(value["slice_ids"]) != count or len(set(value["slice_ids"])) != len(value["slice_ids"]):
        raise ValueError("slice_ids must be unique and equal slice_count")
    normalized = json.loads(_json(value))
    normalized["approved_at"] = _epoch(normalized["approved_at"], "approved_at")
    return normalized


class Store:
    def __init__(self, state_dir: Path | str | None = None):
        self.state_dir = Path(state_dir or os.environ.get("CALLER_PR_GATES_STATE_DIR") or Path.home() / ".local/state/caller-pr-gates")
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.state_dir / "registry.sqlite3"
        with self._connect() as db:
            # Reopening the registry is a read, not another global migration.
            # Native callbacks otherwise rebind every historical PR under the
            # writer lock, competing with all other active chats.
            if db.execute('PRAGMA user_version').fetchone()[0] >= 1:
                self.path.chmod(0o600)
                return
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY, client TEXT NOT NULL, cwd TEXT NOT NULL,
                    parent_session TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prs (
                    repository TEXT NOT NULL, number INTEGER NOT NULL,
                    session_id TEXT NOT NULL, client TEXT NOT NULL, cwd TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(repository, number, session_id)
                );
                CREATE TABLE IF NOT EXISTS dispositions (
                    repository TEXT NOT NULL, number INTEGER NOT NULL, comment_id TEXT NOT NULL,
                    comment_updated_at TEXT NOT NULL, session_id TEXT NOT NULL,
                    receipt TEXT NOT NULL, recorded_at REAL NOT NULL,
                    PRIMARY KEY(repository, number, comment_id, comment_updated_at)
                );
                CREATE TABLE IF NOT EXISTS validation_preflights (repository TEXT,number INTEGER,payload TEXT,PRIMARY KEY(repository,number));
                CREATE TABLE IF NOT EXISTS snapshots (
                    repository TEXT NOT NULL, number INTEGER NOT NULL,
                    snapshot TEXT NOT NULL, gates TEXT NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(repository, number)
                );
                CREATE TABLE IF NOT EXISTS scopes (
                    session_id TEXT PRIMARY KEY, scope TEXT NOT NULL, recorded_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scope_items (
                    session_id TEXT NOT NULL, work_item TEXT NOT NULL, scope TEXT NOT NULL,
                    recorded_at REAL NOT NULL, PRIMARY KEY(session_id,work_item)
                );
                CREATE TABLE IF NOT EXISTS scope_active (
                    session_id TEXT PRIMARY KEY, work_item TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pr_scope_bindings (
                    session_id TEXT NOT NULL, repository TEXT NOT NULL, number INTEGER NOT NULL,
                    scope TEXT NOT NULL, PRIMARY KEY(session_id,repository,number)
                );
                CREATE TABLE IF NOT EXISTS followups (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, repository TEXT NOT NULL,
                    number INTEGER NOT NULL, issue_number INTEGER, kind TEXT NOT NULL,
                    posted_at REAL NOT NULL, due_at REAL NOT NULL, scope TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL, lease_token TEXT, fields TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(session_id, repository, number, kind, posted_at)
                );
                CREATE TABLE IF NOT EXISTS recovery (
                    repository TEXT NOT NULL, number INTEGER NOT NULL,
                    session_id TEXT NOT NULL, fields TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(repository, number, session_id)
                );
                CREATE TABLE IF NOT EXISTS validation_plans (
                    plan_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, repository TEXT NOT NULL,
                    number INTEGER NOT NULL, cwd TEXT NOT NULL, input_digest TEXT NOT NULL,
                    plan TEXT NOT NULL, evidence_digest TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS validation_active_context
                    ON validation_plans(session_id, repository, number, cwd) WHERE active=1;
                CREATE TABLE IF NOT EXISTS validation_runs (
                    run_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, check_id TEXT NOT NULL,
                    input_digest TEXT NOT NULL, status TEXT NOT NULL,
                    exit_code INTEGER, before_digest TEXT NOT NULL, after_digest TEXT,
                    started_at REAL NOT NULL, finished_at REAL, pid INTEGER, process_group INTEGER,
                    CHECK(status IN ('running','pass','fail','unknown'))
                );
                CREATE INDEX IF NOT EXISTS validation_plan_runs
                    ON validation_runs(plan_id, check_id, started_at);
                CREATE TABLE IF NOT EXISTS external_blockers (
                    session_id TEXT NOT NULL, repository TEXT NOT NULL, number INTEGER NOT NULL,
                    head_sha TEXT NOT NULL, fingerprint TEXT NOT NULL, reason TEXT NOT NULL,
                    evidence_url TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    released_at REAL, release_reason TEXT,
                    PRIMARY KEY(session_id, repository, number),
                    CHECK(status IN ('held','released'))
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL,
                    fields TEXT NOT NULL, recorded_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS prs_session ON prs(session_id);
                CREATE INDEX IF NOT EXISTS followups_due ON followups(status, due_at, lease_until);
            """)
            from .monitor import schema
            schema(db)
            db.execute("BEGIN IMMEDIATE")
            # Stamp legacy preflights once, conservatively dating unknown reads
            # at migration. A subsequent full collection can supersede them.
            for pre in db.execute('SELECT * FROM validation_preflights').fetchall():
                payload=json.loads(pre['payload'])
                if 'attempted_at' not in payload:
                    stamp=payload.get('collected_at')
                    payload['attempted_at']=stamp if isinstance(stamp,(int,float)) and not isinstance(stamp,bool) else time.time()
                    db.execute('UPDATE validation_preflights SET payload=? WHERE repository=? AND number=?',(_json(payload),pre['repository'],pre['number']))
            columns = {row["name"] for row in db.execute("PRAGMA table_info(validation_runs)")}
            if "process_group" not in columns:
                db.execute("ALTER TABLE validation_runs ADD COLUMN process_group INTEGER")
            # Existing receipts keep their original scope. Registration below
            # binds new PRs to their selected work item exactly once.
            parents = self._parents(db)
            for pr in db.execute('SELECT session_id,repository,number FROM prs').fetchall():
                root = self._root(pr['session_id'], parents)
                original = db.execute('SELECT scope FROM scopes WHERE session_id=?', (root,)).fetchone()
                if original:
                    db.execute('INSERT OR IGNORE INTO pr_scope_bindings VALUES(?,?,?,?)',
                               (root, pr['repository'], pr['number'], original['scope']))
            db.execute('PRAGMA user_version=1')
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _parents(db):
        return {row["session_id"]: row["parent_session"] for row in db.execute("SELECT session_id, parent_session FROM sessions")}

    @staticmethod
    def _root(session_id, parents):
        seen = set()
        current = session_id
        while parents.get(current):
            if current in seen:
                raise ValueError("session ownership cycle")
            seen.add(current)
            current = parents[current]
        return current

    def _task_sessions(self, db, session_id):
        parents = self._parents(db)
        root = self._root(session_id, parents)
        return {session_id, root} | {sid for sid in parents if self._root(sid, parents) == root}

    @staticmethod
    def _audit(db, event, fields):
        db.execute("INSERT INTO audit(event, fields, recorded_at) VALUES (?, ?, ?)", (event, _json(fields), time.time()))

    def _record_session(self, db, session_id, client, cwd, parent_session):
        if not session_id or not client:
            raise ValueError("session_id and client are required")
        if parent_session == session_id:
            raise ValueError("session cannot own itself")
        parents = self._parents(db)
        previous = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if previous and previous["parent_session"] and parent_session and previous["parent_session"] != parent_session:
            raise ValueError("existing parent ownership cannot be changed")
        parent = parent_session or (previous["parent_session"] if previous else None)
        parents[session_id] = parent
        self._root(session_id, parents)
        # A baseline cannot move into another task after it was recorded.
        if parent and (not previous or previous["parent_session"] != parent) and (db.execute("SELECT 1 FROM scopes WHERE session_id=?", (session_id,)).fetchone() or db.execute("SELECT 1 FROM scope_items WHERE session_id=?", (session_id,)).fetchone()):
            raise ValueError("a scoped task cannot be reparented")
        if parent and (not previous or previous["parent_session"] != parent) and db.execute(
            "SELECT 1 FROM validation_plans WHERE session_id=?", (session_id,)).fetchone():
            raise ValueError("a task with validation evidence cannot be reparented")
        now = time.time()
        db.execute("""INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET cwd=excluded.cwd,
            parent_session=excluded.parent_session, updated_at=excluded.updated_at""",
            (session_id, client, str(cwd), parent, now, now))

    def record_session(self, session_id, client, cwd, parent_session=None):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._record_session(db, session_id, client, cwd, parent_session)

    def session(self, session_id):
        with self._connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["root_session"] = self._root(session_id, self._parents(db))
            return result

    def register(self, session_id, client, repository, number, cwd, parent_session=None):
        if not repository or isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("repository and a positive PR number are required")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._record_session(db, session_id, client, cwd, parent_session)
            now = time.time()
            db.execute("""INSERT INTO prs VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(repository, number, session_id) DO UPDATE SET cwd=excluded.cwd,
                status='active', updated_at=excluded.updated_at""", (repository, number, session_id, client, str(cwd), now, now))
            self._audit(db, "pr_registered", {"session_id": session_id, "repository": repository, "number": number,
                                             "client": client, "cwd": str(cwd)})
            from .monitor import arm
            root = self._root(session_id, self._parents(db))
            baseline = self._scope_row(db, root)
            if baseline:
                db.execute('INSERT OR IGNORE INTO pr_scope_bindings VALUES(?,?,?,?)',
                           (root, repository, number, baseline['scope']))
            owner = db.execute('SELECT * FROM sessions WHERE session_id=?', (root,)).fetchone()
            if owner:
                arm(db, root, owner['client'], repository, number, cwd)
            return dict(db.execute("SELECT * FROM prs WHERE repository=? AND number=? AND session_id=?", (repository, number, session_id)).fetchone())

    def prs(self, session_id=None):
        with self._connect() as db:
            db.execute("BEGIN")
            owned = self._task_sessions(db, session_id) if session_id is not None else None
            # A PR can have several receipts of ownership. Return one row per PR.
            result = {}
            for row in db.execute("SELECT * FROM prs WHERE status='active' ORDER BY created_at, session_id, repository, number"):
                if owned is None or row["session_id"] in owned:
                    key = (row["repository"], row["number"])
                    result.setdefault(key, dict(row))
            return list(result.values())

    def _closed_review_owner(self, db, sid, repo, num):
        """Retirement preserves one canonical receipt owner, not active PR authority."""
        parents = self._parents(db)
        clients = {row['session_id']: row['client'] for row in db.execute('SELECT session_id,client FROM sessions')}
        first = db.execute('''SELECT * FROM prs WHERE repository=? AND number=?
                              ORDER BY created_at, session_id LIMIT 1''', (repo, num)).fetchone()
        watch = db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=?', (repo, num)).fetchone()
        def history(event):
            return [{**json.loads(row['fields']), 'id': row['id'], 'recorded_at': row['recorded_at']}
                    for row in db.execute('''SELECT id, fields, recorded_at FROM audit WHERE event=?
                        AND json_extract(fields,'$.repository')=? AND json_extract(fields,'$.number')=?
                        ORDER BY id''', (event, repo, num))]
        retirements, registrations = history('pr_monitor_retired'), history('pr_registered')
        if (not first or not watch or watch['state'] not in {'closed', 'merged'}
                or not retirements or not registrations or registrations[-1]['id'] > retirements[-1]['id']
                or watch['owner_session'] != sid or self._root(sid, parents) != sid
                or self._root(first['session_id'], parents) != sid
                or watch['client'] != clients.get(sid) or first['client'] != clients.get(first['session_id'])
                or first['status'] != 'retired_' + watch['state']
                or any(not r.get('session_id') or self._root(r['session_id'], parents) != sid for r in registrations)
                or any(r.get('state') != watch['state'] or r.get('head_sha') != retirements[0].get('head_sha')
                       or r.get('gates_waived') is not False for r in retirements)):
            raise ValueError('Closed review receipts require the exact retained owner and retired PR')
        repeated = [r for r in registrations if r['id'] > retirements[0]['id']]
        acknowledgements = []
        anchor = ({'kind': 'registration', 'id': registrations[0]['id'], 'cwd': registrations[0]['cwd']}
                  if registrations[0].get('cwd') else None)
        if repeated:
            # Successful gateway comments/edits register their existing owner.
            # The legacy audit records the verb, not the edit's fields or cwd.
            # This admits a current review receipt, not historical source work.
            for registered in repeated:
                # Registration and its evidence are separate committed writes.
                # A normal retirement may interleave; another registration may
                # not be mistaken for this acknowledgement's evidence.
                boundary = min((r['id'] for r in registrations if r['id'] > registered['id']), default=2**63 - 1)
                proofs = [dict(row) for row in db.execute('''SELECT id, fields FROM audit
                    WHERE event='ownership_evidence' AND id>? AND id<?
                    AND json_extract(fields,'$.target')=? ORDER BY id''',
                    (registered['id'], boundary, f'https://github.com/{repo}/pull/{num}'))]
                proof = json.loads(proofs[0]['fields']) if len(proofs) == 1 else {}
                if (registered['session_id'] != sid or proof.get('session_id') != sid
                        or proof.get('origin') != 'gateway_write'
                        or proof.get('operation') not in (['pr', 'comment'], ['pr', 'edit'])):
                    raise ValueError('Closed review receipts require proven same-owner gateway acknowledgements')
                acknowledgements.append(proofs[0]['id'])
            if not anchor:
                # A legacy plan anchors this owner's declared checkout before
                # closure; it does not reconstruct any registration's cwd.
                plan = db.execute('''SELECT plan_id,cwd FROM validation_plans WHERE session_id=? AND repository=?
                    AND number=? AND created_at<=? ORDER BY created_at, plan_id LIMIT 1''',
                    (sid, repo, num, retirements[0]['recorded_at'])).fetchone()
                anchor = {'kind': 'validation_plan', 'id': plan['plan_id'], 'cwd': plan['cwd']} if plan else None
            if not anchor:
                raise ValueError('Repeated closed registration lacks a pre-retirement checkout anchor')
        if any(r.get('client', clients.get(r['session_id'])) != clients.get(r['session_id']) for r in registrations):
            raise ValueError('Closed review receipts require the original recorded client')
        return {'registration': dict(first), 'retirement': retirements[-1],
                'registration_history': registrations, 'retirement_history': retirements,
                'acknowledgements': acknowledgements,
                'checkout_anchor': anchor or {'kind': 'retained_registration', 'id': registrations[0]['id'], 'cwd': first['cwd']},
                'watch': {key: watch[key] for key in
                ('repository', 'number', 'owner_session', 'client', 'cwd', 'state')}}

    def closed_review_owner(self, sid, repo, num, cwd):
        from .monitor import checkout
        directory = str(Path(cwd).expanduser().resolve())
        with self._connect() as db:
            db.execute('BEGIN')
            identity = self._closed_review_owner(db, sid, repo, num)
        # Resolve the recorded checkout before the write transaction; the final
        # source check is repeated there immediately before the receipt insert.
        recorded = {identity['watch']['cwd'], identity['registration']['cwd'], identity['checkout_anchor']['cwd']}
        if any(checkout(cwd) != directory for cwd in recorded):
            raise ValueError('Closed review receipts require the original recorded checkout')
        return {'repository': repo, 'number': num, 'cwd': directory, 'identity': identity}

    def disposition(self, session_id, repository, number, receipt, *, closed_owner=None):
        if not isinstance(receipt, dict) or not receipt.get("comment_id") or not receipt.get("comment_updated_at"):
            raise ValueError("receipt requires comment_id and comment_updated_at")
        if receipt.get("disposition") not in {"fixed", "nonfix"}:
            raise ValueError("disposition must be fixed or nonfix")
        if not isinstance(receipt.get("reason"), str) or not receipt["reason"].strip():
            raise ValueError("a disposition requires its explanatory reason")
        if receipt["disposition"] == "fixed" and not receipt.get("commit_sha"):
            raise ValueError("a fixed disposition requires commit_sha")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if closed_owner is not None:
                if self._closed_review_owner(db, session_id, repository, number) != closed_owner['identity']:
                    raise ValueError('Retained review ownership changed before recording the receipt')
                from .publication import source, exact_source
                current = source(closed_owner['cwd'], stable=True)
                if (not exact_source(closed_owner, current) or current != closed_owner['source']
                        or current['head'] != receipt.get('closed_head')
                        or current['head'] != closed_owner['identity']['retirement'].get('head_sha')):
                    raise ValueError('Retained review source changed before recording the receipt')
            else:
                owned = self._task_sessions(db, session_id)
                pr_owned = any(row[0] in owned for row in db.execute("SELECT session_id FROM prs WHERE repository=? AND number=? AND status='active'", (repository, number)))
                followup_owned = any(row[0] in owned for row in db.execute("SELECT session_id FROM followups WHERE repository=? AND (number=? OR issue_number=?)", (repository, number, number)))
                if not pr_owned and not followup_owned:
                    raise ValueError("PR or issue is not registered to this task")
            db.execute("""INSERT INTO dispositions VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository, number, comment_id, comment_updated_at)
                DO UPDATE SET session_id=excluded.session_id, receipt=excluded.receipt, recorded_at=excluded.recorded_at""",
                (repository, number, str(receipt["comment_id"]), str(receipt["comment_updated_at"]), session_id, _json(receipt), time.time()))
            self._audit(db, "review_disposition", {"session_id": session_id, "repository": repository, "number": number, "receipt": receipt})

    def dispositions(self, repository, number):
        with self._connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT receipt FROM dispositions WHERE repository=? AND number=? ORDER BY recorded_at", (repository, number))]

    def set_snapshot(self, repository, number, snapshot, gates):
        if snapshot.get('profile') in {'review','validation'}:raise ValueError('Partial evidence cannot replace complete policy snapshots')
        from .publication import reconcile
        reconcile(self,repository,number,snapshot)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(repository, number) DO UPDATE SET snapshot=excluded.snapshot,
                gates=excluded.gates, updated_at=excluded.updated_at""", (repository, number, _json(snapshot), _json(gates), time.time()))
            self._audit(db, "pr_gates_evaluated", {"repository": repository, "number": number, "gates": gates, "head_sha": snapshot.get("head_sha")})

    def set_preflight(self,repository,number,payload):
        payload={**payload,'attempted_at':time.time()}
        with self._connect() as db:
            db.execute('INSERT OR REPLACE INTO validation_preflights VALUES(?,?,?)',(repository,number,_json(payload)))

    def preflight(self,repository,number):
        with self._connect() as db:
            row=db.execute('SELECT payload FROM validation_preflights WHERE repository=? AND number=?',(repository,number)).fetchone()
            payload=json.loads(row[0]) if row else None
            if payload is not None and 'attempted_at' not in payload:
                # A rolling older process can add an undated receipt after the
                # one-time migration. Stamp only that row, preserving any newer
                # concurrent preflight rather than reopening global migration.
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT payload FROM validation_preflights WHERE repository=? AND number=?',(repository,number)).fetchone()
                payload=json.loads(row[0]) if row else None
                if payload is not None and 'attempted_at' not in payload:
                    stamp=payload.get('collected_at')
                    payload['attempted_at']=stamp if isinstance(stamp,(int,float)) and not isinstance(stamp,bool) else time.time()
                    db.execute('UPDATE validation_preflights SET payload=? WHERE repository=? AND number=?',(_json(payload),repository,number))
            return payload

    def snapshot(self, repository, number):
        with self._connect() as db:
            row = db.execute("SELECT * FROM snapshots WHERE repository=? AND number=?", (repository, number)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["snapshot"] = json.loads(result["snapshot"])
            result["gates"] = json.loads(result["gates"])
            return result

    def audit(self, event, fields):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._audit(db, event, fields)

    @staticmethod
    def _validation_plan_row(row):
        if row is None:
            return None
        result = dict(row)
        result["plan"] = json.loads(result["plan"])
        return result

    @staticmethod
    def _validation_checks(plan):
        if not isinstance(plan, dict) or not isinstance(plan.get("checks"), list) or not plan["checks"]:
            raise ValueError("validation plan requires a nonempty checks list")
        normalized = json.loads(_json(plan))
        checks = {}
        commands = set()
        for check in normalized["checks"]:
            if not isinstance(check, dict) or not isinstance(check.get("id"), str) or not check["id"].strip():
                raise ValueError("each validation check requires a nonempty id")
            argv, reason = check.get("argv"), check.get("reason")
            if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg for arg in argv):
                raise ValueError("validation check argv must be a nonempty string list")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("validation check requires its reason")
            cwd = check.get("cwd", ".")
            if not isinstance(cwd, str) or not cwd or Path(cwd).is_absolute():
                raise ValueError("validation check cwd must be a nonempty relative directory")
            identity = _json({"argv": argv, "cwd": Path(cwd).as_posix()})
            if check["id"] in checks or identity in commands:
                raise ValueError("validation checks require unique ids and distinct commands")
            for name in ("env_names", "input_paths"):
                if name in check and (not isinstance(check[name], list) or any(not isinstance(v, str) or not v for v in check[name])):
                    raise ValueError(f"{name} must be a list of nonempty strings")
            if "correction" in check:
                from .progress import correction_metadata
                check["correction"] = correction_metadata(check["correction"])
            checks[check["id"]] = check
            commands.add(identity)
        for name in ("env_names", "input_paths"):
            if name in normalized and (not isinstance(normalized[name], list) or any(not isinstance(v, str) or not v for v in normalized[name])):
                raise ValueError(f"{name} must be a list of nonempty strings")
        return normalized, checks

    def validation_plan(self, sid, repo, num, cwd):
        directory = str(Path(cwd).expanduser().resolve())
        with self._connect() as db:
            db.execute("BEGIN")
            root = self._root(sid, self._parents(db))
            row = db.execute("""SELECT * FROM validation_plans
                WHERE session_id=? AND repository=? AND number=? AND cwd=? AND active=1""",
                (root, repo, num, directory)).fetchone()
            return self._validation_plan_row(row)

    def validation_plans(self, session_id):
        with self._connect() as db:
            db.execute("BEGIN")
            root = self._root(session_id, self._parents(db))
            return [self._validation_plan_row(row) for row in db.execute(
                "SELECT * FROM validation_plans WHERE session_id=? AND active=1 ORDER BY created_at, plan_id", (root,))]

    def validation_runs(self, plan_id):
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM validation_runs WHERE plan_id=? ORDER BY started_at, run_id", (plan_id,))]

    def _validation_evidence(self, db, row):
        _, checks = self._validation_checks(row["plan"])
        inputs = {key: value for key, value in row["plan"].items() if key != "checks"}
        evidence = []
        for candidate in db.execute("""SELECT r.*, p.plan AS original_plan FROM validation_runs r
            JOIN validation_plans p ON p.plan_id=r.plan_id
            WHERE p.session_id=? AND p.repository=? AND p.number=? AND p.cwd=?
            AND p.input_digest=? ORDER BY r.started_at, r.run_id""",
            (row["session_id"], row["repository"], row["number"], row["cwd"],
             row["input_digest"])):
            run = dict(candidate)
            original, original_checks = self._validation_checks(json.loads(run.pop("original_plan")))
            if ({key: value for key, value in original.items() if key != "checks"} == inputs
                    and run["check_id"] in checks
                    and original_checks.get(run["check_id"]) == checks[run["check_id"]]
                    and run["input_digest"] == checks[run["check_id"]].get("input_digest", row["input_digest"])):
                evidence.append(run)
        return evidence

    def validation_evidence(self, plan_id):
        """Read original runs with identical inputs, check declarations and ownership."""
        with self._connect() as db:
            row = self._validation_plan_row(db.execute(
                "SELECT * FROM validation_plans WHERE plan_id=?", (plan_id,)).fetchone())
            if row is None:
                raise ValueError("validation plan not found")
            return self._validation_evidence(db, row)

    def _retained_validation_plan(self, db, sid, repo, num, directory):
        """A retired watch retains authority only for its exact original plan owner."""
        from .monitor import checkout
        watch = db.execute('SELECT * FROM pr_watch WHERE repository=? AND number=?', (repo, num)).fetchone()
        first_owner = db.execute('''SELECT session_id FROM prs WHERE repository=? AND number=?
                                    ORDER BY created_at, session_id LIMIT 1''', (repo, num)).fetchone()
        first_plan = db.execute('''SELECT cwd FROM validation_plans WHERE session_id=? AND repository=?
                                   AND number=? ORDER BY created_at, plan_id LIMIT 1''', (sid, repo, num)).fetchone()
        row = db.execute('''SELECT * FROM validation_plans WHERE session_id=? AND repository=?
                            AND number=? AND cwd=? AND active=1''', (sid, repo, num, directory)).fetchone()
        # Re-registering a closed PR can rearm its watch. It cannot manufacture
        # a new qualification owner/checkout from a legacy foreign receipt.
        if (not watch or watch['owner_session'] != sid or watch['state'] not in {'open', 'merged'}
                or not first_owner or self._root(first_owner['session_id'], self._parents(db)) != sid
                or not first_plan or first_plan['cwd'] != directory
                or checkout(watch['cwd']) != directory or not row):
            raise ValueError('Post-merge validation requires the exact recorded owner, checkout and retained plan')
        return self._validation_plan_row(row)

    def retained_validation_plan(self, sid, repo, num, cwd):
        directory = str(Path(cwd).expanduser().resolve())
        with self._connect() as db:
            db.execute('BEGIN')
            return self._retained_validation_plan(db, sid, repo, num, directory)

    def _final_coverage_context(self, db, sid, repo, original, final):
        if original == final:
            raise ValueError('Final coverage requires a distinct delivered successor PR')
        result = {'session_id': sid, 'repository': repo}
        for role, number in (('original', original), ('final', final)):
            identity = self._closed_review_owner(db, sid, repo, number)
            rows = db.execute('''SELECT * FROM validation_plans WHERE session_id=? AND repository=?
                                 AND number=? AND active=1''', (sid, repo, number)).fetchall()
            scope = db.execute('SELECT scope FROM pr_scope_bindings WHERE session_id=? AND repository=? AND number=?',
                               (sid, repo, number)).fetchone()
            if identity['watch']['state'] != 'merged' or len(rows) != 1 or not scope:
                raise ValueError('Final coverage requires both retained merged PRs, their exact plans and immutable scopes')
            plan = self._validation_plan_row(rows[0])
            recorded = [identity[key]['cwd'] for key in ('registration', 'watch', 'checkout_anchor')]
            if any(str(Path(path).expanduser().resolve()) != plan['cwd'] for path in recorded):
                raise ValueError('Final coverage requires each plan at its own recorded checkout')
            if db.execute('''SELECT 1 FROM validation_runs r JOIN validation_plans p ON p.plan_id=r.plan_id
                             WHERE p.session_id=? AND p.repository=? AND p.number=? AND r.status='running' ''',
                          (sid, repo, number)).fetchone():
                raise ValueError('Final coverage cannot conclude while either PR has an active execution')
            result[role] = {'identity': identity, 'row': plan, 'scope': json.loads(scope['scope']),
                'runs': [dict(r) for r in db.execute('SELECT * FROM validation_runs WHERE plan_id=? ORDER BY started_at,run_id',
                                                    (plan['plan_id'],))],
                'dispositions': [json.loads(r[0]) for r in db.execute('''SELECT receipt FROM dispositions
                    WHERE repository=? AND number=? ORDER BY recorded_at''', (repo, number))]}
        return result

    def final_coverage_context(self, sid, repo, original, final):
        with self._connect() as db:
            db.execute('BEGIN')
            return self._final_coverage_context(db, sid, repo, original, final)

    def record_final_coverage(self, context, proof, source_guard):
        """Append one conclusion; never update registrations, plans or executions."""
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            actual = self._final_coverage_context(db, context['session_id'], context['repository'],
                context['original']['row']['number'], context['final']['row']['number'])
            if actual != context:
                raise ValueError('Final coverage ownership, scope, plan, results or review receipts changed during collection')
            source_guard()
            fields = {'context': context, 'proof': proof}
            self._audit(db, 'validation_final_coverage', fields)
            return {'id': db.execute('SELECT last_insert_rowid()').fetchone()[0], **fields}

    def final_coverage_receipt(self, sid, plan_id):
        with self._connect() as db:
            row = db.execute('''SELECT id,fields FROM audit WHERE event='validation_final_coverage'
                AND json_extract(fields,'$.context.session_id')=?
                AND (json_extract(fields,'$.context.original.row.plan_id')=?
                     OR json_extract(fields,'$.context.final.row.plan_id')=?) ORDER BY id DESC LIMIT 1''',
                (sid, plan_id, plan_id)).fetchone()
            return {'id': row['id'], **json.loads(row['fields'])} if row else None

    def _checker_corrections(self, db, existing, plan, checks, old_checks):
        """Validate fresh correction links inside the same plan-write transaction."""
        from .progress import private_checker_correction
        added = [check for key, check in checks.items() if key not in old_checks and "correction" in check]
        if not added:
            return []
        evidence = self._validation_evidence(db, existing)
        def has_run(identifier):
            return db.execute("""SELECT 1 FROM validation_runs r JOIN validation_plans p ON p.plan_id=r.plan_id
                WHERE p.session_id=? AND p.repository=? AND p.number=? AND p.cwd=? AND r.check_id=?""",
                (existing["session_id"], existing["repository"], existing["number"], existing["cwd"], identifier)).fetchone() is not None
        receipts = []
        for check in added:
            correction = check["correction"]
            target = correction["check_id"]
            runs = [run for run in evidence if run["check_id"] == target]
            failed = runs[-1] if runs else None
            if correction.get("unexecuted") is True:
                if target not in old_checks or has_run(target):
                    raise ValueError("An unexecuted checker correction requires an existing required check without any run history")
                previous = {"original_input_digest": old_checks[target].get("input_digest", existing["input_digest"])}
            elif (target not in old_checks or not failed or failed["run_id"] != correction["run_id"]
                    or failed["status"] != "fail" or type(failed["exit_code"]) is not int or failed["exit_code"] <= 0
                    or failed["finished_at"] is None
                    or failed["before_digest"] != failed["input_digest"] or failed["after_digest"] != failed["input_digest"]):
                raise ValueError("A checker correction must cite the current actual failed run of an existing required check")
            else:
                previous = {"failed_run_id": failed["run_id"], "failed_exit_code": failed["exit_code"],
                            "original_input_digest": failed["input_digest"]}
            if sum(c.get("correction", {}).get("check_id") == target for c in checks.values()) != 1:
                raise ValueError("This checker already has a correction; correct its defective successor instead of branching")
            if has_run(check["id"]):
                raise ValueError("A checker correction requires a fresh check identity without prior results")
            pins = private_checker_correction(existing["cwd"], plan, check, old_checks[target])
            receipts.append({"source_plan_id": existing["plan_id"], "check_id": target,
                **previous, "corrected_check_id": check["id"],
                "corrected_input_digest": check["input_digest"], **correction, **pins})
        return receipts

    def save_validation_plan(self, sid, repo, num, cwd, plan, input_digest, evidence_digest, *, retained=False, source_epoch=None):
        if not isinstance(input_digest, str) or not input_digest.strip():
            raise ValueError("validation input_digest is required")
        if not isinstance(evidence_digest, str):
            raise ValueError("validation evidence_digest must be a string")
        normalized, checks = self._validation_checks(plan)
        if source_epoch is not None and not retained:
            raise ValueError('A final-head epoch requires retained qualification')
        if not retained and any("input_digest" in check or "correction" in check for check in checks.values()):
            raise ValueError("Pinned qualification controls require explicit merged-source validation")
        directory = str(Path(cwd).expanduser().resolve())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            parents = self._parents(db)
            root = self._root(sid, parents)
            owned = self._task_sessions(db, sid)
            if retained:
                previous = self._retained_validation_plan(db, sid, repo, num, directory)
                if input_digest != previous['input_digest'] and source_epoch is None:
                    raise ValueError('Post-merge validation inputs changed; source repairs need their own scoped PR')
            elif not any(row[0] in owned for row in db.execute(
                "SELECT session_id FROM prs WHERE repository=? AND number=? AND status='active'", (repo, num))):
                raise ValueError("PR is not registered to this task")
            row = db.execute("""SELECT * FROM validation_plans
                WHERE session_id=? AND repository=? AND number=? AND cwd=? AND active=1""",
                (root, repo, num, directory)).fetchone()
            existing = self._validation_plan_row(row)
            now = time.time()
            if normalized.get('source_epoch') != (existing or {}).get('plan', {}).get('source_epoch'):
                raise ValueError('Stored source epoch provenance is immutable and cannot be caller supplied')
            if source_epoch is not None:
                from .progress import plan_declaration
                if (not existing or 'source_epoch' in existing['plan']
                        or source_epoch['previous_plan_id'] != existing['plan_id']
                        or source_epoch['previous_input_digest'] != existing['input_digest']
                        or source_epoch['previous_check_digests'] != {c['id']: c['input_digest'] for c in existing['plan']['checks'] if 'input_digest' in c}
                        or input_digest == existing['input_digest']
                        or plan_declaration(normalized) != plan_declaration(existing['plan'])):
                    raise ValueError('Retained plan changed before final-head epoch admission')
                normalized['source_epoch'] = source_epoch
            if existing:
                _, old_checks = self._validation_checks(existing["plan"])
                if not set(old_checks).issubset(checks):
                    raise ValueError("validation plan cannot remove previously required checks")
                for name in ("input_paths", "env_names"):
                    if not set(existing["plan"].get(name, [])).issubset(normalized.get(name, [])):
                        raise ValueError(f"validation plan cannot remove previously declared {name}")
                    for identifier, old_check in old_checks.items():
                        if not set(old_check.get(name, [])).issubset(checks[identifier].get(name, [])):
                            raise ValueError(f"validation check cannot remove previously declared {name}")
                same_input = input_digest == existing["input_digest"]
                same_evidence = evidence_digest == existing["evidence_digest"]
                if same_input and same_evidence and normalized == existing["plan"]:
                    return existing
                if db.execute("SELECT 1 FROM validation_runs WHERE plan_id=? AND status='running'",
                              (existing["plan_id"],)).fetchone():
                    raise ValueError("validation plan has an active run; finish or verify its process exited")
                if same_input:
                    if any(checks[key] != old_checks[key] for key in old_checks):
                        raise ValueError("existing validation checks are immutable for unchanged inputs")
                    old_settings = {key: value for key, value in existing["plan"].items() if key != "checks"}
                    new_settings = {key: value for key, value in normalized.items() if key != "checks"}
                    if old_settings != new_settings:
                        raise ValueError("validation input/environment settings are immutable for unchanged inputs")
                for correction in self._checker_corrections(db, existing, normalized, checks, old_checks):
                    self._audit(db, "validation_checker_correction", correction)
                if same_input and same_evidence:
                    failed = db.execute("SELECT 1 FROM validation_runs WHERE plan_id=? AND status='fail'",
                                        (existing["plan_id"],)).fetchone()
                    added = set(checks) - set(old_checks)
                    controls = retained and added and all("input_digest" in checks[key] for key in added)
                    if not added or not (failed or controls):
                        raise ValueError("unchanged validation plan is frozen; new input/evidence is required")
                    # A real failure can justify a distinct investigation, retaining
                    # the failed check and all completed evidence in this generation.
                    # Explicit merged qualification may also append independently
                    # pinned controls. Neither path resets an existing result.
                    db.execute("UPDATE validation_plans SET plan=?, updated_at=? WHERE plan_id=?",
                               (_json(normalized), now, existing["plan_id"]))
                    self._audit(db, "validation_plan_extended", {"plan_id": existing["plan_id"], "check_ids": list(checks)})
                    existing.update(plan=normalized, updated_at=now)
                    return existing
                if same_input and (not evidence_digest.strip() or same_evidence):
                    raise ValueError("resetting unchanged inputs requires new nonempty remote evidence")
                db.execute("UPDATE validation_plans SET active=0, updated_at=? WHERE plan_id=?",
                           (now, existing["plan_id"]))
            plan_id = str(uuid.uuid4())
            db.execute("""INSERT INTO validation_plans
                (plan_id,session_id,repository,number,cwd,input_digest,plan,evidence_digest,active,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
                (plan_id, root, repo, num, directory, input_digest, _json(normalized), evidence_digest, now, now))
            self._audit(db, "validation_plan_recorded", {"plan_id": plan_id, "session_id": root,
                "repository": repo, "number": num, "input_digest": input_digest, "evidence_digest": evidence_digest})
            if source_epoch is not None:
                self._audit(db, 'validation_final_head_epoch', {**source_epoch, 'plan_id': plan_id,
                    'input_digest': input_digest,
                    'check_digests': {key: check.get('input_digest', input_digest) for key, check in checks.items()}})
            return self._validation_plan_row(db.execute("SELECT * FROM validation_plans WHERE plan_id=?", (plan_id,)).fetchone())

    def begin_validation(self, plan_id, check_id, input_digest, owner_pid=None, owner_group=None, *, session_id=None, retained=False):
        pid = os.getpid() if owner_pid is None else owner_pid
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("validation owner_pid must be a positive integer")
        if owner_group is not None and (isinstance(owner_group, bool) or not isinstance(owner_group, int) or owner_group <= 0):
            raise ValueError("validation owner_group must be a positive integer or None")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM validation_plans WHERE plan_id=? AND active=1", (plan_id,)).fetchone()
            plan = self._validation_plan_row(row)
            if not plan:
                raise ValueError("validation plan is missing or no longer active")
            if session_id is not None and self._root(session_id, self._parents(db)) != plan["session_id"]:
                raise ValueError("validation plan is owned by another task")
            if retained:
                self._retained_validation_plan(db, session_id, plan['repository'], plan['number'], plan['cwd'])
            _, checks = self._validation_checks(plan["plan"])
            if check_id not in checks:
                raise ValueError("check is absent from the active required validation plan")
            if any(c.get("correction", {}).get("unexecuted") is True
                   and c["correction"]["check_id"] == check_id for c in checks.values()):
                raise ValueError("This unexecuted checker has an admitted correction; execute its fresh corrected check")
            if "input_digest" in checks[check_id] and not retained:
                raise ValueError("Pinned qualification controls require explicit merged-source validation")
            if input_digest != checks[check_id].get("input_digest", plan["input_digest"]):
                raise ValueError("validation input changed; save an updated plan first")
            if db.execute("SELECT 1 FROM validation_runs WHERE plan_id=? AND status='running'", (plan_id,)).fetchone():
                raise ValueError("validation already has an active run")
            if any(run["check_id"] == check_id and run["status"] in {"running", "pass", "fail"}
                   for run in self._validation_evidence(db, plan)):
                raise ValueError("this check already completed on unchanged input/evidence; do not repeat it")
            run_id, now = str(uuid.uuid4()), time.time()
            db.execute("""INSERT INTO validation_runs
                (run_id,plan_id,check_id,input_digest,status,exit_code,before_digest,after_digest,started_at,finished_at,pid,process_group)
                VALUES (?,?,?,?,'running',NULL,?,NULL,?,NULL,?,?)""",
                (run_id, plan_id, check_id, input_digest, input_digest, now, pid, owner_group))
            self._audit(db, "validation_started", {"run_id": run_id, "plan_id": plan_id, "check_id": check_id, "pid": pid, "process_group": owner_group})
            return dict(db.execute("SELECT * FROM validation_runs WHERE run_id=?", (run_id,)).fetchone())

    def finish_validation(self, run_id, exit_code, after_digest, *, session_id=None, source_valid=True):
        if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
            raise ValueError("validation exit_code must be an integer or None")
        if after_digest is not None and not isinstance(after_digest, str):
            raise ValueError("validation after_digest must be a string or None")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM validation_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("validation run not found")
            result = dict(row)
            if session_id is not None:
                owner = db.execute("SELECT session_id FROM validation_plans WHERE plan_id=?", (result["plan_id"],)).fetchone()
                if not owner or self._root(session_id, self._parents(db)) != owner[0]:
                    raise ValueError("validation run is owned by another task")
            if result["status"] != "running":
                if result["exit_code"] == exit_code and result["after_digest"] == after_digest:
                    return result
                raise ValueError("completed validation evidence cannot be rewritten")
            unchanged = bool(after_digest and after_digest == result["before_digest"])
            status = "unknown" if exit_code is None or exit_code < 0 or not unchanged or source_valid is not True else ("pass" if exit_code == 0 else "fail")
            now = time.time()
            db.execute("""UPDATE validation_runs SET status=?, exit_code=?, after_digest=?, finished_at=?
                WHERE run_id=?""", (status, exit_code, after_digest, now, run_id))
            self._audit(db, "validation_finished", {"run_id": run_id, "status": status, "exit_code": exit_code,
                "before_digest": result["before_digest"], "after_digest": after_digest, "source_valid": source_valid})
            result.update(status=status, exit_code=exit_code, after_digest=after_digest, finished_at=now)
            return result

    def recover_validation_run(self, run_id):
        """Only a verified exited owner process can release an unfinished run."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM validation_runs WHERE run_id=? AND status='running'", (run_id,)).fetchone()
            if not row:
                return False
            pid, group = row["pid"], row["process_group"]
            if group is not None and (not isinstance(group, int) or group <= 0):
                return False
            if group is None and (not isinstance(pid, int) or pid <= 0):
                return False
            try:
                if group is not None:
                    os.killpg(group, 0)
                else:
                    os.kill(pid, 0)
            except ProcessLookupError:
                pass
            except (PermissionError, OSError):
                return False  # Unknown process state never authorizes a duplicate run.
            else:
                return False
            now = time.time()
            db.execute("UPDATE validation_runs SET status='unknown', finished_at=? WHERE run_id=?", (now, run_id))
            self._audit(db, "validation_interrupted_owner_exited", {"run_id": run_id, "pid": pid, "process_group": group})
            return True

    def _external_owner(self, db, sid, repo, num):
        if not isinstance(sid, str) or not sid or not isinstance(repo, str) or not repo or isinstance(num, bool) or not isinstance(num, int) or num < 1:
            raise ValueError("external blocker requires a task and valid repository/PR identity")
        owned = self._task_sessions(db, sid)
        if not any(row[0] in owned for row in db.execute(
                "SELECT session_id FROM prs WHERE repository=? AND number=? AND status='active'", (repo, num))):
            raise ValueError("PR is not registered to this task")
        return self._root(sid, self._parents(db))

    def record_external_blocker(self, sid, repo, num, head_sha, fingerprint, reason, evidence_url):
        """Persist the classifier's verified external handoff, never completion."""
        for name, value in (("head_sha", head_sha), ("fingerprint", fingerprint), ("reason", reason), ("evidence_url", evidence_url)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"external blocker requires nonempty {name}")
        parsed = urlparse(evidence_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("external blocker evidence must be a usable HTTPS URL")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            root = self._external_owner(db, sid, repo, num)
            row = db.execute("SELECT * FROM external_blockers WHERE session_id=? AND repository=? AND number=?",
                             (root, repo, num)).fetchone()
            if row and row["status"] == "held":
                if row["head_sha"] == head_sha and row["fingerprint"] == fingerprint:
                    return dict(row)  # Repeated Stops neither re-audit nor overwrite evidence.
                raise ValueError("release the previous handoff with new authoritative evidence before replacing it")
            now = time.time()
            db.execute("""INSERT INTO external_blockers
                (session_id,repository,number,head_sha,fingerprint,reason,evidence_url,status,created_at,updated_at,released_at,release_reason)
                VALUES (?,?,?,?,?,?,?,'held',?,?,NULL,NULL)
                ON CONFLICT(session_id,repository,number) DO UPDATE SET
                    head_sha=excluded.head_sha, fingerprint=excluded.fingerprint, reason=excluded.reason,
                    evidence_url=excluded.evidence_url, status='held', created_at=excluded.created_at,
                    updated_at=excluded.updated_at, released_at=NULL, release_reason=NULL""",
                (root, repo, num, head_sha, fingerprint, reason, evidence_url, now, now))
            self._audit(db, "external_blocker_held", {"session_id": root, "repository": repo, "number": num,
                "head_sha": head_sha, "fingerprint": fingerprint, "reason": reason, "evidence_url": evidence_url,
                "obligation_complete": False})
            return dict(db.execute("SELECT * FROM external_blockers WHERE session_id=? AND repository=? AND number=?",
                                   (root, repo, num)).fetchone())

    def external_blocker(self, sid, repo, num):
        with self._connect() as db:
            db.execute("BEGIN")
            root = self._root(sid, self._parents(db))
            owned = self._task_sessions(db, sid)
            if not any(row[0] in owned for row in db.execute(
                    "SELECT session_id FROM prs WHERE repository=? AND number=? AND status='active'", (repo, num))):
                return None
            row = db.execute("""SELECT * FROM external_blockers
                WHERE session_id=? AND repository=? AND number=? AND status='held'""", (root, repo, num)).fetchone()
            return dict(row) if row else None

    def release_external_blocker(self, sid, repo, num, *, explicit_retry=False, reason=None, head_sha=None, fingerprint=None):
        """Reopen work after operator retry or classifier-verified changed evidence."""
        if not isinstance(explicit_retry, bool):
            raise ValueError("explicit_retry must be a boolean")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            root = self._external_owner(db, sid, repo, num)
            row = db.execute("""SELECT * FROM external_blockers
                WHERE session_id=? AND repository=? AND number=? AND status='held'""", (root, repo, num)).fetchone()
            if not row:
                return False
            if explicit_retry:
                release_reason = "explicit_retry"
            else:
                if reason != "new_authoritative_evidence":
                    return False
                if not isinstance(head_sha, str) or not head_sha.strip() or not isinstance(fingerprint, str) or not fingerprint.strip():
                    return False
                if head_sha == row["head_sha"] and fingerprint == row["fingerprint"]:
                    return False
                release_reason = "new_authoritative_evidence"
            now = time.time()
            db.execute("""UPDATE external_blockers SET status='released', updated_at=?, released_at=?, release_reason=?
                WHERE session_id=? AND repository=? AND number=?""", (now, now, release_reason, root, repo, num))
            self._audit(db, "external_blocker_released", {"session_id": root, "repository": repo, "number": num,
                "previous_head_sha": row["head_sha"], "previous_fingerprint": row["fingerprint"],
                "reason": release_reason, "head_sha": head_sha, "fingerprint": fingerprint, "obligation_complete": False})
            return True

    def correct_false_ownership(self, sid, repo, num, evidence):
        """Audit an operator-proven read-only misattribution, preserving other owners."""
        if not isinstance(evidence, dict) or evidence.get("confirmed_read_only") is not True:
            raise ValueError("ownership correction requires confirmed read-only evidence")
        reason, audit_ids, raw_file = evidence.get("reason"), evidence.get("audit_ids"), evidence.get("evidence_file")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("ownership correction requires its reason")
        if not isinstance(audit_ids, list) or not audit_ids or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in audit_ids):
            raise ValueError("ownership correction requires positive audit_ids")
        if not isinstance(raw_file, str) or not Path(raw_file).is_absolute() or not Path(raw_file).is_file():
            raise ValueError("ownership correction requires an existing absolute evidence_file")
        proof = Path(raw_file).read_text()
        if sid not in proof or repo not in proof or any(str(identifier) not in proof for identifier in audit_ids):
            raise ValueError("evidence file does not document this session/target and cited audit records")
        target = f"https://github.com/{repo}/pull/{num}"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM prs WHERE session_id=? AND repository=? AND number=?", (sid, repo, num)).fetchone()
            if not row:
                raise ValueError("exact session/PR ownership record not found")
            if row["status"] == "misattributed_read_only":
                return False
            if row["status"] != "active":
                raise ValueError("only an active misattributed ownership record can be corrected")
            owned = self._task_sessions(db, sid)
            root = self._root(sid, self._parents(db))
            placeholders = ",".join("?" for _ in owned)
            if db.execute(f"SELECT 1 FROM dispositions WHERE repository=? AND number=? AND session_id IN ({placeholders})",
                          (repo, num, *owned)).fetchone():
                raise ValueError("recorded review dispositions contradict read-only ownership")
            if db.execute("SELECT 1 FROM validation_plans WHERE session_id=? AND repository=? AND number=?", (root, repo, num)).fetchone():
                raise ValueError("recorded validation work contradicts read-only ownership")
            cited = set(audit_ids)
            found = set()
            for audit in db.execute("SELECT id,event,fields FROM audit ORDER BY id"):
                fields = json.loads(audit["fields"])
                matching = (fields.get("repository") == repo and fields.get("number") == num) or fields.get("target", "").rstrip("/") == target
                owner = fields.get("session_id")
                if audit["id"] in cited:
                    if not matching or owner != sid:
                        raise ValueError("cited audit record does not belong to the exact correction target")
                    found.add(audit["id"])
                if owner not in owned or not matching:
                    continue
                if audit["event"] in {"github_mutation", "review_thread_resolved", "review_disposition"}:
                    raise ValueError("actual target writes contradict read-only ownership")
                if audit["event"] == "ownership_evidence":
                    origin = fields.get("origin")
                    if origin == "explicit_register":
                        # A forced registration after an erroneous hook denial can
                        # be corrected only when the operator documents that fact.
                        if audit["id"] not in cited or not any(marker in proof.lower() for marker in ("hook-induced", "hook_induced")):
                            raise ValueError("explicit registration needs documented hook-induced evidence")
                    elif origin not in {"read_only", "legacy_discovery"}:
                        raise ValueError("actual local/GitHub target writes contradict read-only ownership")
            if found != cited:
                raise ValueError("one or more ownership evidence audit records are missing")
            now = time.time()
            db.execute("UPDATE prs SET status='misattributed_read_only',updated_at=? WHERE session_id=? AND repository=? AND number=?",
                       (now, sid, repo, num))
            self._audit(db, "false_ownership_corrected", {"session_id": sid, "repository": repo, "number": num,
                "previous_status": row["status"], "status": "misattributed_read_only", "evidence": evidence,
                "evidence_sha256": hashlib.sha256(proof.encode()).hexdigest(), "obligations_complete": False})
            return True

    def recovery(self, repository, number, session_id):
        with self._connect() as db:
            db.execute("BEGIN")
            root = self._root(session_id, self._parents(db))
            row = db.execute("SELECT * FROM recovery WHERE repository=? AND number=? AND session_id=?", (repository, number, root)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result.update(json.loads(result.pop("fields")))
            return result

    def record_recovery(self, repository, number, session_id, fields):
        if not isinstance(fields, dict) or set(fields).intersection({"repository", "number", "session_id", "created_at", "updated_at"}):
            raise ValueError("recovery fields cannot change ownership or timestamps")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owned = self._task_sessions(db, session_id)
            if not any(row[0] in owned for row in db.execute("SELECT session_id FROM prs WHERE repository=? AND number=? AND status='active'", (repository, number))):
                raise ValueError("PR is not registered to this task")
            root = self._root(session_id, self._parents(db))
            old = db.execute("SELECT * FROM recovery WHERE repository=? AND number=? AND session_id=?", (repository, number, root)).fetchone()
            merged = json.loads(old["fields"]) if old else {}
            merged.update(fields)
            now = time.time()
            db.execute("""INSERT INTO recovery VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository, number, session_id) DO UPDATE SET fields=excluded.fields,
                updated_at=excluded.updated_at""", (repository, number, root, _json(merged), now, now))
            self._audit(db, "pr_recovery_updated", {"repository": repository, "number": number, "session_id": root, "fields": fields})
            row = db.execute("SELECT * FROM recovery WHERE repository=? AND number=? AND session_id=?", (repository, number, root)).fetchone()
            result = dict(row)
            result.update(json.loads(result.pop("fields")))
            return result

    @staticmethod
    def _scope_row(db, root, work_item=None):
        if work_item is None:
            active = db.execute('SELECT work_item FROM scope_active WHERE session_id=?', (root,)).fetchone()
            work_item = active['work_item'] if active else 'original'
        if work_item == 'original':
            return db.execute('SELECT scope FROM scopes WHERE session_id=?', (root,)).fetchone()
        return db.execute('SELECT scope FROM scope_items WHERE session_id=? AND work_item=?', (root, work_item)).fetchone()

    def record_scope(self, session_id, scope_dict, *, work_item=None):
        scope = _scope(scope_dict)
        if work_item is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', work_item):
            raise ValueError('work-item must be a stable identifier of at most 128 characters')
        if work_item not in {None, 'original'} and (not isinstance(scope.get('approval_evidence'), str) or not scope['approval_evidence'].strip()):
            raise ValueError('a new work-item needs approval_evidence quoting its actual human authorization')
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            root = self._root(session_id, self._parents(db))
            if work_item is None:
                active = db.execute('SELECT work_item FROM scope_active WHERE session_id=?', (root,)).fetchone()
                work_item = active['work_item'] if active else 'original'
            row = self._scope_row(db, root, work_item)
            if row:
                if row[0] != _json(scope):
                    raise ValueError('approved scope baseline is immutable; use --work-item ID for another explicitly authorized work item')
            elif work_item == 'original':
                db.execute('INSERT INTO scopes VALUES(?,?,?)', (root, _json(scope), time.time()))
                self._audit(db, 'scope_baseline_recorded', {'session_id': root, 'scope': scope})
                parents = self._parents(db)
                for pr in db.execute('SELECT session_id,repository,number FROM prs').fetchall():
                    if self._root(pr['session_id'], parents) == root:
                        db.execute('INSERT OR IGNORE INTO pr_scope_bindings VALUES(?,?,?,?)', (root, pr['repository'], pr['number'], _json(scope)))
            else:
                db.execute('INSERT INTO scope_items VALUES(?,?,?,?)', (root, work_item, _json(scope), time.time()))
                self._audit(db, 'work_item_scope_recorded', {'session_id': root, 'work_item': work_item, 'scope': scope})
            db.execute('INSERT INTO scope_active VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET work_item=excluded.work_item', (root, work_item))

    def get_scope(self, session_id, *, work_item=None):
        with self._connect() as db:
            db.execute("BEGIN")
            root = self._root(session_id, self._parents(db))
            row = self._scope_row(db, root, work_item)
            return json.loads(row[0]) if row else None

    def scope_baselines(self, session_id):
        with self._connect() as db:
            root = self._root(session_id, self._parents(db))
            rows = db.execute('SELECT scope FROM scopes WHERE session_id=? UNION ALL SELECT scope FROM scope_items WHERE session_id=?', (root, root)).fetchall()
            return [json.loads(row['scope']) for row in rows]

    def followup_scope(self, job):
        """Verify the immutable baseline pinned to this job, independent of selection."""
        encoded = _json(job.get('scope'))
        return next((scope for scope in self.scope_baselines(job['session_id']) if _json(scope) == encoded), None)

    def pr_scope(self, session_id, repository, number):
        with self._connect() as db:
            root = self._root(session_id, self._parents(db))
            row = db.execute('SELECT scope FROM pr_scope_bindings WHERE session_id=? AND repository=? AND number=?', (root, repository, number)).fetchone()
            return json.loads(row['scope']) if row else None

    @staticmethod
    def _followup(row):
        result = dict(row)
        for name in ("scope", "fields"):
            result[name] = json.loads(result[name])
        return result

    def register_followup(self, session_id, repository, number, posted_at, scope, kind="prd", issue_number=None, publication_notice=False):
        posted_at = _epoch(posted_at, "posted_at")
        if kind not in {"prd", "slice"}:
            raise ValueError("followup kind must be prd or slice")
        if publication_notice and kind != 'prd':
            raise ValueError('publication notice requires a PRD')
        if not repository or isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("repository and a positive artifact number are required")
        if issue_number is not None and (isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number < 1):
            raise ValueError("issue_number must be a positive integer")
        scope = _scope(scope) if scope else {}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            root = self._root(session_id, self._parents(db))
            previous = db.execute('SELECT * FROM followups WHERE session_id=? AND repository=? AND number=? AND kind=? AND posted_at=?', (root, repository, number, kind, posted_at)).fetchone()
            if previous:
                if previous['scope'] != _json(scope) or previous['issue_number'] != issue_number:
                    raise ValueError('existing followup identity cannot change its scope or artifact')
                return self._followup(previous)
            baseline = self._scope_row(db, root)
            if baseline and _json(scope) != baseline[0]:
                raise ValueError("followup scope must match the immutable task baseline")
            now = time.time()
            identifier = str(uuid.uuid4())
            fields = {}
            if publication_notice:
                owner = db.execute('SELECT client FROM sessions WHERE session_id=?', (root,)).fetchone()
                if not owner or owner['client'] not in {'codex', 'claude'}:
                    raise ValueError('PRD review notice requires a recorded root-chat client')
                fields = {'prd_review_notice': {'client': owner['client'], 'state': 'pending'}}
            db.execute("""INSERT OR IGNORE INTO followups
                (id, session_id, repository, number, issue_number, kind, posted_at, due_at, scope, status, fields, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (identifier, root, repository, number, issue_number, kind, posted_at, posted_at + 1800, _json(scope), _json(fields), now, now))
            row = db.execute("SELECT * FROM followups WHERE session_id=? AND repository=? AND number=? AND kind=? AND posted_at=?", (root, repository, number, kind, posted_at)).fetchone()
            if row["scope"] != _json(scope) or row["issue_number"] != issue_number:
                raise ValueError("existing followup identity cannot change its scope or artifact")
            self._audit(db, "followup_registered", {"id": row["id"], "session_id": root, "due_at": row["due_at"]})
            return self._followup(row)

    def consume_prd_notices(self, session_id, client):
        """Claim new gateway publication notices once, only in their root chat.

        Old followups have no receipt. Neither retries nor document edits create
        one. This is native additional context, never a background delivery job.
        """
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM followups WHERE session_id=? AND kind='prd' AND json_extract(fields,'$.prd_review_notice.state')='pending' LIMIT 1", (session_id,)).fetchone():
                return []
            db.execute('BEGIN IMMEDIATE')
            owner = db.execute('SELECT client FROM sessions WHERE session_id=?', (session_id,)).fetchone()
            if not owner or owner['client'] != client or self._root(session_id, self._parents(db)) != session_id:
                return []
            result = []
            for row in db.execute("SELECT * FROM followups WHERE session_id=? AND kind='prd' ORDER BY created_at,id", (session_id,)).fetchall():
                fields = json.loads(row['fields'])
                notice = fields.get('prd_review_notice') or {}
                if notice.get('state') != 'pending':
                    continue
                # The recorded owner also repairs pending receipts created by
                # the older gateway's default --client flag, without replay.
                notice.update(client=client, state='emitted', emitted_at=time.time())
                db.execute('UPDATE followups SET fields=?,updated_at=? WHERE id=?', (_json(fields), time.time(), row['id']))
                self._audit(db, 'prd_review_notice_emitted', {'id': row['id'], 'session_id': session_id, 'client': client})
                result.append(self._followup(row))
            return result

    def followups(self, session_id=None, due_only=False):
        with self._connect() as db:
            db.execute("BEGIN")
            owned = self._task_sessions(db, session_id) if session_id is not None else None
            now = time.time()
            result = []
            for row in db.execute("SELECT * FROM followups ORDER BY due_at, id"):
                if owned is not None and row["session_id"] not in owned:
                    continue
                if due_only and (row["status"] == "complete" or row["due_at"] > now or (row["status"] == "running" and row["lease_until"] is not None and row["lease_until"] > now)):
                    continue
                result.append(self._followup(row))
            return result

    def retry_followup(self, id):
        """Explicit operator retry. Never steal a live lease or reopen completed work."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = db.execute("SELECT fields FROM followups WHERE id=?", (id,)).fetchone()
            if row is None:
                return False
            fields = json.loads(row["fields"])
            # Operator retry authorizes a new delivery generation. Preserve review
            # evidence, but the previous queue ACK/receipt cannot suppress it.
            for name in ("delivery", "prompt", "queued_prompt_hash", "delivery_receipt", "dispatch_at",
                         "acknowledged_at", "runner_exit", "next_attempt_at", "error"):
                fields.pop(name, None)
            changed = db.execute("""UPDATE followups SET status='pending', attempts=0,
                lease_until=NULL, lease_token=NULL, fields=?, updated_at=?
                WHERE id=? AND status!='complete'
                AND (status!='running' OR lease_until IS NULL OR lease_until<=?)""", (_json(fields), now, id, now)).rowcount
            if not changed:
                return False
            self._audit(db, "followup_explicit_retry", {"id": id})
            return True

    def note_followup_delivery(self, id, session_id, turn_id, prompt_hash):
        """Record an owned queue prompt being consumed, independently of its lease.

        Queue acceptance is not consumption or completion. Only the exact current
        prompt can acknowledge this dispatch; older retries cannot acknowledge it.
        """
        if not isinstance(prompt_hash, str) or len(prompt_hash) != 64:
            return False
        if turn_id is not None and not isinstance(turn_id, str):
            return False
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM followups WHERE id=?", (id,)).fetchone()
            if row is None or row["status"] == "complete" or row["session_id"] != session_id:
                return False
            fields = json.loads(row["fields"])
            prompt = fields.get("prompt")
            if fields.get("delivery") != "queue_existing_thread" or not isinstance(prompt, str):
                return False
            expected = hashlib.sha256(prompt.encode()).hexdigest()
            if prompt_hash != expected or (fields.get("queued_prompt_hash") or expected) != expected:
                return False
            old = fields.get("delivery_receipt") or {}
            if old.get("prompt_hash") == prompt_hash and old.get("dispatch_at") == fields.get("dispatch_at"):
                return True
            now = time.time()
            receipt = {"session_id": session_id, "turn_id": turn_id or "", "prompt_hash": prompt_hash,
                       "consumed_at": now, "dispatch_at": fields.get("dispatch_at")}
            fields["delivery_receipt"] = receipt
            db.execute("UPDATE followups SET fields=?, updated_at=? WHERE id=?", (_json(fields), now, id))
            self._audit(db, "followup_delivery_consumed", {"id": id, **receipt})
            return True

    def claim_followup(self, id, lease_seconds=900):
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive and finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            token = str(uuid.uuid4())
            changed = db.execute("""UPDATE followups SET status='running', attempts=attempts+1,
                lease_token=?, lease_until=?, updated_at=?
                WHERE id=? AND due_at<=? AND status!='complete'
                AND (status!='running' OR lease_until IS NULL OR lease_until<=?)""",
                (token, now + lease_seconds, now, id, now, now)).rowcount
            if not changed:
                return None
            self._audit(db, "followup_claimed", {"id": id, "lease_token": token, "lease_until": now + lease_seconds})
            return self._followup(db.execute("SELECT * FROM followups WHERE id=?", (id,)).fetchone())

    def renew_followup(self, id, lease_token, lease_seconds=900):
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive and finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            return bool(db.execute("""UPDATE followups SET lease_until=?, updated_at=?
                WHERE id=? AND status='running' AND lease_token=? AND lease_until>?""",
                (now + lease_seconds, now, id, lease_token, now)).rowcount)

    def complete_followup(self, id, lease_token, fields=None):
        return self._update_followup(id, "complete", fields, lease_token, lease_required=True)

    def update_followup(self, id, status, fields=None):
        fields = dict(fields or {})
        token = fields.pop("lease_token", None)
        return self._update_followup(id, status, fields, token, lease_required=False)

    def _update_followup(self, id, status, fields, token, lease_required):
        if status not in _STATUSES:
            raise ValueError("invalid followup status")
        fields = dict(fields or {})
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM followups WHERE id=?", (id,)).fetchone()
            if row is None:
                return False
            if row["status"] == "complete":
                return status == "complete" and token == row["lease_token"]
            now = time.time()
            if lease_required or row["status"] == "running" or status in {"running", "complete"}:
                if row["status"] != "running" or not token or token != row["lease_token"] or row["lease_until"] is None or row["lease_until"] <= now:
                    return False
            merged = json.loads(row["fields"])
            merged.update(fields)
            lease_until = row["lease_until"] if status == "running" else None
            db.execute("UPDATE followups SET status=?, fields=?, lease_until=?, updated_at=? WHERE id=?", (status, _json(merged), lease_until, now, id))
            self._audit(db, "followup_updated", {"id": id, "status": status, "fields": fields})
            return True


