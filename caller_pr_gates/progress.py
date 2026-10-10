"""Content-bound local validation plans; a commit alone is not a new input."""
from __future__ import annotations
from .prompts import render_prompt

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import time


_CHECK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
REMOTE_EVIDENCE_TTL = 120
_FORBIDDEN_RUNNERS = {"sh", "bash", "zsh", "fish", "csh", "tcsh", "dash", "cmd", "cmd.exe",
                      "powershell", "powershell.exe", "pwsh", "true", "false", "echo", "printf"}
_VALIDATION_COMMAND = re.compile(
    r"(?:^|[;&|\n]\s*|\s)(?:"
    r"(?:python(?:\d+(?:\.\d+)*)?\s+-m\s+)?(?:pytest|unittest|compileall|mypy|ruff|flake8|pylint|coverage)\b"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test(?::[\w-]+)?|check(?::[\w-]+)?|lint(?::[\w-]+)?|typecheck|build)\b"
    r"|(?:cargo|go|dotnet|gradle|mvn)\s+(?:test|check|clippy|verify|build)\b"
    r"|(?:make|just)\s+(?:test|check|lint|typecheck|validate|verify|build)(?:[-_:][\w-]+)?\b"
    r"|(?:vitest|jest|tsc|eslint|rubocop|rspec)\b"
    r"|swift\s+test\b|(?:tox|nox)\b"
    r"|ctest\b|cmake\s+--build\b)", re.I)


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("validation paths must remain inside the working directory")
    return resolved


def _runner(argv: list[str], cwd: Path, root: Path):
    first = Path(argv[0]).name.lower()
    if first in _FORBIDDEN_RUNNERS or any(re.search(r"&&|\|\||[;`\n\r]|\$\(", argument) or argument in {"|", ">", ">>", "<", "&"} for argument in argv):
        raise ValueError("validation commands cannot use shell/no-op runners or shell control operators")
    interpreter = re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|node|nodejs|ruby|perl", first)
    if interpreter and any(re.match(r"-(?:c|e)(?:$|.)|--(?:eval|print)(?:=|$)", argument) for argument in argv[1:]):
        raise ValueError("inline programs cannot substitute for real validation runners")
    if validation_command(" ".join(shlex.quote(argument) for argument in argv)):
        return
    executable = Path(argv[0])
    if not executable.is_absolute():
        executable = cwd / executable
    script = argv[1] if interpreter and len(argv) > 1 and not argv[1].startswith("-") else None
    if script:
        candidate = _inside(cwd / script, root)
        if candidate.is_file():
            return
    if executable.exists():
        candidate = _inside(executable, root)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return
    raise ValueError("validation command must name a common check runner or an actual repository script")


def normalize_plan(payload: dict, cwd: str | Path) -> dict:
    """Freeze necessary commands and their reasons before any validation runs.

    Commands use argv, not shell text, so an exited check cannot be hidden by a
    pipeline or background shell operator. Optional input_paths add ignored
    files/directories that the declared check depends on.
    """
    if not isinstance(payload, dict) or set(payload) - {"checks", "input_paths", "env_names"}:
        raise ValueError("validation plan must contain checks and optional input_paths")
    root = Path(cwd).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("validation working directory does not exist")
    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("validation plan requires at least one necessary check")
    normalized, identifiers = [], set()
    for check in checks:
        if not isinstance(check, dict) or set(check) - {"id", "argv", "reason", "cwd"}:
            raise ValueError("checks require id, argv, reason and optional cwd")
        identifier, argv, reason = check.get("id"), check.get("argv"), check.get("reason")
        if not isinstance(identifier, str) or not _CHECK_ID.fullmatch(identifier) or identifier in identifiers:
            raise ValueError("check IDs must be unique nonempty identifiers")
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv)):
            raise ValueError("each check needs a nonempty argv of nonempty strings")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("each check needs its concrete validation reason")
        relative = check.get("cwd", ".")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError("check cwd must be a relative directory")
        directory = _inside(root / relative, root)
        if not directory.is_dir():
            raise ValueError("check working directory does not exist")
        _runner(argv, directory, root)
        identifiers.add(identifier)
        normalized.append({"id": identifier, "argv": list(argv), "reason": reason.strip(),
                           "cwd": directory.relative_to(root).as_posix() or "."})
    inputs = payload.get("input_paths", [])
    if not isinstance(inputs, list) or any(not isinstance(path, str) or not path for path in inputs):
        raise ValueError("input_paths must be relative file or directory paths")
    paths = []
    for value in inputs:
        path = Path(value)
        if path.is_absolute():
            raise ValueError("input_paths must remain relative to the working directory")
        candidate = _inside(root / path, root)
        if candidate == root or candidate.name == ".git" or ".git" in candidate.relative_to(root).parts:
            raise ValueError("input_paths cannot include the whole workspace or Git metadata")
        paths.append(candidate.relative_to(root).as_posix())
    environment = payload.get("env_names", [])
    if (not isinstance(environment, list)
            or any(not isinstance(name, str) or not _ENV_NAME.fullmatch(name) for name in environment)):
        raise ValueError("env_names must contain environment variable names, not values")
    return {"checks": normalized, "input_paths": sorted(set(paths)), "env_names": sorted(set(environment))}


def plan_digest(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError("validation fingerprint requires a readable Git working tree")
    return result.stdout


def repo_root(cwd: str | Path) -> str:
    working = Path(cwd).expanduser().resolve()
    return str(Path(os.fsdecode(_git(working, "rev-parse", "--show-toplevel")).strip()).resolve())


def working_state(cwd: str | Path) -> dict:
    root = Path(repo_root(cwd))
    head = os.fsdecode(_git(root, "rev-parse", "HEAD")).strip()
    changes = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    return {"head": head, "clean": not changes, "root": str(root)}


def _js_tokens(source):
    """Small non-executing lexer for literal tools.exec_command arguments."""
    tokens, index = [], 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    while index < len(source):
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            index = len(source) if end < 0 else end + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            index = len(source) if end < 0 else end + 2
            continue
        if character in {"'", '"', "`"}:
            quote, value, dynamic = character, [], False
            index += 1
            closed = False
            while index < len(source):
                character = source[index]
                if character == quote:
                    index += 1
                    closed = True
                    break
                if quote == "`" and source.startswith("${", index):
                    dynamic = True
                if character == "\\" and index + 1 < len(source):
                    escaped = source[index + 1]
                    if escaped in {"u", "x"}:
                        count = 4 if escaped == "u" else 2
                        encoded = source[index + 2:index + 2 + count]
                        if len(encoded) == count and re.fullmatch(r"[0-9a-fA-F]+", encoded):
                            value.append(chr(int(encoded, 16)))
                            index += count + 2
                            continue
                    if escaped not in {"\n", "\r"}:
                        value.append(escapes.get(escaped, escaped))
                    index += 2
                    continue
                value.append(character)
                index += 1
            tokens.append(("literal" if closed and not dynamic else "dynamic", "".join(value)))
            continue
        match = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", source[index:])
        if match:
            value = match.group(0)
            tokens.append(("identifier", value))
            index += len(value)
        else:
            tokens.append(("punctuation", character))
            index += 1
    return tokens


def literal_exec_commands(source: str) -> list[dict]:
    """Extract literal cmd/workdir from actual tools.exec_command calls.

    Quoted program text and JS comments are ignored. Dynamic object values,
    aliases and interpolated templates cannot be inferred and are not claimed
    to be enforced by this recognizer.
    """
    if not isinstance(source, str) or "exec_command" not in source:
        return []
    tokens = _js_tokens(source)
    result = []
    for offset in range(len(tokens) - 4):
        if tokens[offset:offset + 5] != [("identifier", "tools"), ("punctuation", "."),
                                       ("identifier", "exec_command"), ("punctuation", "("), ("punctuation", "{")]:
            continue
        fields, depth, index = {}, 1, offset + 5
        while index < len(tokens) and depth:
            kind, value = tokens[index]
            if kind == "punctuation" and value == "{":
                depth += 1
            elif kind == "punctuation" and value == "}":
                depth -= 1
            elif (depth == 1 and kind in {"identifier", "literal"} and value in {"cmd", "command", "cwd", "workdir"}
                  and index + 3 < len(tokens) and tokens[index + 1] == ("punctuation", ":")
                  and tokens[index + 2][0] == "literal" and tokens[index + 3] in {("punctuation", ","), ("punctuation", "}")}):
                fields[value] = tokens[index + 2][1]
                index += 2
            index += 1
        command = fields.get("cmd") or fields.get("command")
        if command:
            row = {"command": command}
            directory = fields.get("workdir") or fields.get("cwd")
            if directory:
                row["cwd"] = directory
            result.append(row)
    return result


def validation_command(command: str) -> bool:
    """Conservative routing hint for common runners, never success evidence.

    Shell strings, aliases and arbitrary scripts are not decidable from regex;
    declared plans and the authoritative runner provide actual enforcement.
    Heredoc bodies are data and must not accidentally trigger this hint.
    """
    if not isinstance(command, str):
        return False
    lines, delimiter = [], None
    for line in command.splitlines():
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            continue
        lines.append(line)
        match = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if match:
            delimiter = match.group(1)
    source = "\n".join(lines)
    wrapped = literal_exec_commands(source)
    if any(validation_command(row["command"]) for row in wrapped):
        return True
    # Inspect command positions, rather than quoted messages or filenames in a
    # read-only command. This remains a routing hint, not a shell interpreter.
    try:
        lexer = shlex.shlex(source, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    groups, current = [], []
    for token in tokens:
        if token and all(character in ";&|()\n" for character in token):
            if current:
                groups.append(current)
                current = []
        else:
            current.append(token)
    if current:
        groups.append(current)
    for arguments in groups:
        while arguments and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", arguments[0]):
            arguments = arguments[1:]
        if arguments and Path(arguments[0]).name == "env":
            arguments = arguments[1:]
            while arguments and (arguments[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", arguments[0])):
                arguments = arguments[1:]
        while len(arguments) > 1 and Path(arguments[0]).name in {"uv", "poetry", "pipenv"} and arguments[1] == "run":
            arguments = arguments[2:]
        if not arguments or Path(arguments[0]).name in {"echo", "printf", "cat", "rg", "grep", "sed", "git"}:
            continue
        # Starting the expression at executable position prevents arguments to
        # a different runner (for example echo) from impersonating a check.
        if _VALIDATION_COMMAND.match(" ".join([Path(arguments[0]).name, *arguments[1:]])):
            return True
        executable = Path(arguments[0]).name
        script = (arguments[1] if len(arguments) > 1 and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|node|nodejs|ruby|perl", executable)
                  and not arguments[1].startswith("-") else arguments[0])
        if re.match(r"^(?:test|check|validate|verify|lint|build)(?:[_.-]|$)", Path(script).name, re.I):
            return True
    return False


def fingerprint(cwd: str | Path, plan: dict) -> str:
    """Hash current source, not HEAD, timestamps, caches, or test output.

    Every tracked and untracked nonignored repository file is included. Commit
    bookkeeping with identical content preserves evidence. A check can include
    otherwise ignored inputs explicitly. The caller must compare this digest
    before and after waiting for the actual process exit.
    """
    working = Path(cwd).expanduser().resolve()
    normalized = normalize_plan(plan, working)
    repository = Path(repo_root(working))
    paths = {os.fsdecode(path) for path in _git(repository, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\x00") if path}
    explicit = set()
    for value in normalized["input_paths"]:
        candidate = working / value
        if candidate.is_dir() and not candidate.is_symlink():
            for directory, folders, files in os.walk(candidate, followlinks=False):
                folders[:] = [folder for folder in folders if folder != ".git"]
                for name in files + [folder for folder in folders if (Path(directory) / folder).is_symlink()]:
                    paths.add((Path(directory) / name).relative_to(repository).as_posix())
        else:
            relative = candidate.relative_to(repository).as_posix()
            paths.add(relative)
            explicit.add(relative)
    digest = hashlib.sha256()
    def append(value: bytes):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    append(str(working).encode())
    # Commands/reasons bind separately to a frozen plan row. Adding a necessary
    # check or rewording its reason must never manufacture changed source.
    # Never store or print environment values; only their digest contributes.
    for name in normalized["env_names"]:
        append(name.encode())
        append(b"unset" if name not in os.environ else b"set" + os.environ[name].encode())
    for value in sorted(paths):
        path = repository / value
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            # A deleted tracked file is already absent from current source;
            # staging/committing that deletion must not manufacture new input.
            if value in explicit:
                append(os.fsencode(value))
                append(b"missing")
            continue
        append(os.fsencode(value))
        append(str(stat.S_IMODE(metadata.st_mode)).encode())
        if path.is_symlink():
            append(b"symlink")
            append(os.fsencode(os.readlink(path)))
            # The file used by a check may be reached through a symlink. Hash
            # its actual current content too, including targets outside Git.
            if path.is_file():
                append(path.read_bytes())
            elif path.exists():
                raise ValueError("directory symlink inputs require explicit file inputs")
        elif path.is_file():
            append(b"file")
            append(path.read_bytes())
        elif path.is_dir() and (path / ".git").exists():
            # Submodule source can change independently of the parent gitlink.
            # Refuse incomplete evidence instead of silently passing it.
            raise ValueError("submodule inputs require an explicit validation workspace")
        else:
            raise ValueError("validation input is not a readable ordinary file")
    return digest.hexdigest()


def validation_decision(plan: dict, runs: list[dict], current_digest: str, check_id: str | None = None) -> dict:
    """Pure local decision; publication and review proof remain separate gates.

    Run records use check_id, input_digest, status and exit_code, plus
    before_digest/after_digest when complete. A yielded tool or an active process never
    provides passing evidence. Previous failed checks remain runnable.
    """
    checks = {check["id"] for check in plan["checks"]}
    if check_id is not None and check_id not in checks:
        return {"allowed": False, "phase": "VALIDATE", "reason": "Check is not in the frozen necessary-check plan"}
    successful, active = set(), set()
    for run in runs:
        if run.get("check_id") not in checks or run.get("input_digest") != current_digest:
            continue
        if run.get("status") == "running":
            active.add(run["check_id"])
        elif (run.get("status") == "pass" and type(run.get("exit_code")) is int
              and run["exit_code"] == 0 and run.get("before_digest") == current_digest
              and run.get("after_digest") == current_digest):
            successful.add(run["check_id"])
    phase = "PUBLISH_REQUIRED" if checks.issubset(successful) and not active else "VALIDATE"
    if check_id in active:
        return {"allowed": False, "phase": phase, "reason": "This validation is still running; wait for its actual exit"}
    if check_id in successful or phase == "PUBLISH_REQUIRED":
        return {"allowed": False, "phase": phase, "reason": "Validation already passed for unchanged inputs; publish the fix and conclude its review comments"}
    return {"allowed": True, "phase": phase, "reason": "Necessary validation has no successful current-input result",
            "passed": sorted(successful), "pending": sorted(checks - successful)}


def evaluate_progress(row: dict, runs: list[dict], current_digest: str,
                      snapshot: dict | None = None, review_gates: list[dict] | None = None,
                      local_state: dict | None = None) -> dict:
    """Order validation, publication and proven review conclusions.

    A run may pass only after its managed process actually exited successfully
    on unchanged before/after inputs. Remote merge state is never completion
    proof. Missing or stale remote evidence stays WAIT_REMOTE.
    """
    def result(phase, reason, **fields):
        actions = {"INPUTS_CHANGED": render_prompt('validation_inputs_changed'),
                   "VALIDATE_REQUIRED": render_prompt('validation_required'),
                   "VALIDATE_RUNNING": render_prompt('validation_running'),
                   "VALIDATE_FAILED": render_prompt('validation_failed'),
                   "PUBLISH_REQUIRED": render_prompt('validation_publish'),
                   "SYNC_BASE_REQUIRED": render_prompt('validation_sync_base'),
                   "CONCLUDE_REQUIRED": render_prompt('validation_conclude'),
                   "ADDRESS_REQUIRED": render_prompt('validation_address'),
                   "REMOTE_READ_REQUIRED": render_prompt('validation_remote_read'),
                   "WAIT_REMOTE": render_prompt('validation_wait_remote'),
                   "COMPLETE": render_prompt('validation_complete')}
        return {"phase": phase, "blocked": phase != "COMPLETE", "next_action": actions[phase],
                "reason": reason, **fields}
    def fresh_remote():
        if not isinstance(snapshot, dict):
            return False
        collected = snapshot.get("collected_at")
        return (isinstance(collected, (int, float)) and not isinstance(collected, bool)
                and 0 <= time.time() - collected <= REMOTE_EVIDENCE_TTL)
    gates = {gate.get("gate"): gate for gate in (review_gates or []) if isinstance(gate, dict)}
    # A truthful conclusion is owed as soon as handling is already proven. A
    # new local edit is not permission to postpone that public conclusion.
    review = gates.get("REVIEW_HANDLED")
    if review:
        obligations = (review.get("evidence") or {}).get("obligations")
        if isinstance(obligations, list):
            pending = [str(item.get("id")) for item in obligations
                       if isinstance(item, dict) and item.get("handled") is True and item.get("concluded") is not True]
            if pending:
                if not fresh_remote():
                    return result("REMOTE_READ_REQUIRED", "Previously handled comments may still need conclusions; verify their current live state before more validation")
                return result("CONCLUDE_REQUIRED", "Publish a truthful fix/non-fix reply and close the already handled inline review threads", pending=pending)
    if not isinstance(row, dict) or not row.get("input_digest") or row["input_digest"] != current_digest:
        return result("INPUTS_CHANGED", "Source or declared validation inputs changed; record a current-input plan")
    # Publish first. Local validation is evidence for completion, never a gate
    # on exposing the change to remote CI and reviewers.
    local_state = local_state or {}
    head = local_state.get('head')
    if not isinstance(head, str) or not head or not isinstance(local_state.get('clean'), bool):
        return result('PUBLISH_REQUIRED', 'Inspect, commit and push the current work before local checks')
    if not local_state['clean']:
        return result('PUBLISH_REQUIRED', 'Commit and push current work now; local checks must not delay publication')
    if not fresh_remote():
        return result('REMOTE_READ_REQUIRED', 'Verify the remote PR head before running local checks')
    if snapshot.get('head_sha') != head:
        return result('PUBLISH_REQUIRED', 'Push the current commit now; local checks must not delay publication')
    from .policy import branch_freshness_gate
    freshness = branch_freshness_gate(snapshot)
    if freshness['status'] == 'FAIL':
        return result('SYNC_BASE_REQUIRED', '; '.join(freshness['details']))
    if freshness['status'] != 'PASS':
        return result('REMOTE_READ_REQUIRED', '; '.join(freshness['details']))
    plan = row.get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("checks"), list) or not plan["checks"]:
        return result("VALIDATE_REQUIRED", "Necessary-check plan is missing or malformed")
    checks = {check.get("id") for check in plan["checks"] if isinstance(check, dict)}
    if None in checks or len(checks) != len(plan["checks"]):
        return result("VALIDATE_REQUIRED", "Necessary-check identities are missing or duplicated")
    latest = {}
    active = set()
    for run in runs:
        if run.get("check_id") not in checks or run.get("input_digest") != current_digest:
            continue
        latest[run["check_id"]] = run
        if run.get("status") == "running":
            active.add(run["check_id"])
    failed, passed, missing = [], [], []
    for identifier in sorted(checks):
        run = latest.get(identifier)
        if run is None:
            missing.append(identifier)
        elif identifier in active:
            continue
        elif (run.get("status") == "pass" and type(run.get("exit_code")) is int
              and run["exit_code"] == 0 and run.get("before_digest") == current_digest
              and run.get("after_digest") == current_digest):
            passed.append(identifier)
        elif run.get("status") == "fail":
            failed.append(identifier)
        else:
            missing.append(identifier)
    if failed:
        return result("VALIDATE_FAILED", "Fix the actual failed check, push the fix promptly, then rerun its required validation", failed=failed, pending=sorted(failed + missing), passed=passed, running=sorted(active))
    if missing:
        return result("VALIDATE_REQUIRED", "Run the distinct missing necessary validations", pending=missing, passed=passed, running=sorted(active))
    if active:
        return result("VALIDATE_RUNNING", "Managed checks are running; work on the next authorized PR and let the monitor check remote state", running=sorted(active), pending=[], passed=passed)
    if snapshot.get("head_sha_after") != head:
        return result("WAIT_REMOTE", "The remote head changed or is unverifiable; collect fresh current-head evidence")
    if not isinstance(review_gates, list):
        return result("WAIT_REMOTE", "Collect current review and CI evidence after publication")
    if gates.get("REVIEW_HANDLED", {}).get("status") == "FAIL":
        return result("ADDRESS_REQUIRED", "Published work still needs actual comment dispositions; do not invent handling evidence or repeat passed local checks")
    required = {"PR_NOT_DRAFT", "CI_FAILED", "CI_PENDING", "REVIEW_HANDLED", "REVIEW_CONCLUDED", "MERGE_CONFLICT"}
    if not required.issubset(gates) or any(gates[name].get("status") != "PASS" for name in required):
        return result("WAIT_REMOTE", "Current review/CI gates still require evidence or action; successful local checks must not be repeated")
    return result("COMPLETE", "Current published head, every necessary local check and every separate PR gate are verified")
