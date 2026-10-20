"""Content-bound local validation plans; a commit alone is not a new input."""
from __future__ import annotations
from .prompts import render_prompt

import ast
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
_SOURCE_BLOB_BATCH_BYTES = 8 * 1024 * 1024
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


def _python_script_index(argv):
    """Locate a literal Python script, optionally after bare env assignments."""
    interpreter = 0
    if argv and argv[0] == "env":
        interpreter = 1
        while interpreter < len(argv):
            name, separator, _ = argv[interpreter].partition("=")
            if not separator or not _ENV_NAME.fullmatch(name):
                break
            interpreter += 1
        if interpreter == 1:
            return None
    if (len(argv) > interpreter + 1
            and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(argv[interpreter]).name)
            and not argv[interpreter + 1].startswith("-")):
        return interpreter + 1
    return None


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
    # Locate an env-wrapped script without treating its trailing arguments as
    # Python options or changing the existing direct-runner guards above.
    script_index = _python_script_index(argv)
    script = (argv[script_index] if script_index is not None else
              argv[1] if interpreter and len(argv) > 1 and not argv[1].startswith("-") else None)
    if script:
        candidate = _inside(cwd / script, root)
        if candidate.is_file():
            return
    if executable.exists():
        candidate = _inside(executable, root)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return
    raise ValueError("validation command must name a common check runner or an actual repository script")


def _ordinary_control_path(path, root):
    # Resolving a private path would otherwise discard a link's identity.
    # Keep this restriction local to the new per-check qualification surface.
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink():
            raise ValueError("Qualification control paths cannot traverse symlinks; pin ordinary local files")


def _input_settings(payload, root, *, control=False):
    inputs = payload.get("input_paths", [])
    if not isinstance(inputs, list) or any(not isinstance(path, str) or not path for path in inputs):
        raise ValueError("input_paths must be relative file or directory paths")
    paths = []
    for value in inputs:
        path = Path(value)
        if path.is_absolute():
            raise ValueError("input_paths must remain relative to the working directory")
        if control:
            _ordinary_control_path(root / path, root)
        candidate = _inside(root / path, root)
        if candidate == root or candidate.name == ".git" or ".git" in candidate.relative_to(root).parts:
            raise ValueError("input_paths cannot include the whole workspace or Git metadata")
        paths.append(candidate.relative_to(root).as_posix())
    environment = payload.get("env_names", [])
    if (not isinstance(environment, list)
            or any(not isinstance(name, str) or not _ENV_NAME.fullmatch(name) for name in environment)):
        raise ValueError("env_names must contain environment variable names, not values")
    return {"input_paths": sorted(set(paths)), "env_names": sorted(set(environment))}


def correction_metadata(value):
    """Diagnose an actual failed or explicitly unexecuted checker, never waive it."""
    keys = {"check_id", "diagnosis", "evidence_path"}
    if (not isinstance(value, dict) or set(value) not in (keys | {"run_id"}, keys | {"unexecuted"})
            or any(not isinstance(value[key], str) or not value[key].strip() for key in keys)
            or not _CHECK_ID.fullmatch(value["check_id"])
            or ("unexecuted" in value and value["unexecuted"] is not True)
            or ("run_id" in value and (not isinstance(value["run_id"], str)
                or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value["run_id"])))):
        raise ValueError("correction requires check_id, diagnosis, evidence_path and either actual failed run_id or unexecuted: true")
    return {key: item.strip() if isinstance(item, str) else item for key, item in sorted(value.items())}


def private_checker_correction(cwd, plan, check, original):
    """Admit a separately pinned Python entrypoint; retain its entire obligation."""
    root = Path(cwd).resolve()
    correction = correction_metadata(check.get("correction"))
    old, new = original["argv"], check["argv"]
    script_index = _python_script_index(old)
    if (script_index is None or len(new) != len(old) or new[script_index].startswith("-")
            or new[:script_index] != old[:script_index] or new[script_index + 1:] != old[script_index + 1:]
            or check.get("cwd", ".") != original.get("cwd", ".")
            or check["reason"] != original["reason"]):
        raise ValueError("A checker correction must preserve its literal env prefix, Python interpreter, arguments, cwd and acceptance reason")
    for name in ("input_paths", "env_names"):
        if not set(original.get(name, [])).issubset(check.get(name, [])):
            raise ValueError("A checker correction must retain every original check dependency and environment name")
    if not re.fullmatch(r"[0-9a-f]{64}", str(check.get("input_digest"))):
        raise ValueError("A checker correction requires a newly pinned qualification control")

    def private_file(value):
        path = root / value
        _ordinary_control_path(path, root)
        path = _inside(path, root)
        relative = path.relative_to(root).as_posix()
        if not path.is_file() or ".git" in path.relative_to(root).parts:
            raise ValueError("Checker correction files must be ordinary private files inside the recorded checkout")
        ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "--quiet", "--", relative],
                                 capture_output=True, timeout=10)
        if ignored.returncode != 0:
            raise ValueError("Checker correction files must be ignored private inputs; tracked changes need a scoped product PR")
        return path

    def included(path, paths):
        return any(path == root / value or (root / value).is_dir() and path.is_relative_to(root / value)
                   for value in paths)

    directory = Path(original.get("cwd", "."))
    before, after = private_file(directory / old[script_index]), private_file(directory / new[script_index])
    evidence = private_file(correction["evidence_path"])
    if (before == after or not included(before, plan.get("input_paths", []) + original.get("input_paths", []))
            or not included(after, check.get("input_paths", []))
            or not included(evidence, check.get("input_paths", [])) or evidence in {before, after}):
        raise ValueError("Retain the pinned original driver and separately pin a new driver and diagnosis evidence file")
    old_body, new_body, evidence_body = before.read_bytes(), after.read_bytes(), evidence.read_bytes()
    if not evidence_body.strip():
        raise ValueError("Checker correction diagnosis evidence must be nonempty")
    try:
        unchanged = ast.dump(ast.parse(old_body)) == ast.dump(ast.parse(new_body))
    except (SyntaxError, ValueError) as exc:
        raise ValueError("Checker correction requires readable Python drivers") from exc
    if unchanged:
        raise ValueError("An unchanged, copied or comment-only checker is not a meaningful correction")
    return {"original_driver": before.relative_to(root).as_posix(),
            "corrected_driver": after.relative_to(root).as_posix(),
            "original_driver_sha256": hashlib.sha256(old_body).hexdigest(),
            "corrected_driver_sha256": hashlib.sha256(new_body).hexdigest(),
            "evidence_sha256": hashlib.sha256(evidence_body).hexdigest()}


def normalize_plan(payload: dict, cwd: str | Path, *, stored=False) -> dict:
    """Freeze necessary commands and their reasons before any validation runs.

    Commands use argv, not shell text, so an exited check cannot be hidden by a
    pipeline or background shell operator. Optional input_paths add ignored
    files/directories that the declared check depends on.
    """
    allowed = {"checks", "input_paths", "env_names"} | ({"source_epoch"} if stored else set())
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("validation plan must contain checks and optional input_paths")
    root = Path(cwd).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("validation working directory does not exist")
    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("validation plan requires at least one necessary check")
    shared = _input_settings(payload, root)
    normalized, identifiers = [], set()
    for check in checks:
        allowed = {"id", "argv", "reason", "cwd", "input_paths", "env_names", "correction"}
        if stored:
            allowed.add("input_digest")
        if not isinstance(check, dict) or set(check) - allowed:
            raise ValueError("checks require id, argv, reason and optional cwd/input_paths/env_names; input pins are recorded by the runner")
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
        if "correction" in check:
            _ordinary_control_path(root / relative, root)
        directory = _inside(root / relative, root)
        if not directory.is_dir():
            raise ValueError("check working directory does not exist")
        _runner(argv, directory, root)
        identifiers.add(identifier)
        normalized.append({"id": identifier, "argv": list(argv), "reason": reason.strip(),
                           "cwd": directory.relative_to(root).as_posix() or "."})
        if "correction" in check:
            normalized[-1]["correction"] = correction_metadata(check["correction"])
            if not check.get("input_paths"):
                raise ValueError("A checker correction requires explicit private input_paths")
        if "input_paths" in check or "env_names" in check:
            normalized[-1].update(_input_settings(check, root, control=True))
            first = Path(argv[0]).name.lower()
            script_index = _python_script_index(argv)
            if (script_index is None and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|node|nodejs|ruby|perl", first)
                    and len(argv) > 1 and not argv[1].startswith("-")):
                script_index = 1
            candidate = directory / argv[script_index if script_index is not None else 0]
            if script_index is not None or candidate.is_relative_to(root):
                _ordinary_control_path(candidate, root)
            candidate = _inside(candidate, root) if script_index is not None else candidate.resolve()
            driver_inputs = normalized[-1]["input_paths"]
            if argv[0] == "env" and script_index is not None:
                # Retain an already shared env-driver pin without rewriting
                # a frozen check's separate dependency declaration.
                driver_inputs = driver_inputs + shared["input_paths"]
            if (candidate.is_file() and candidate.is_relative_to(root)
                    and not any(candidate == root / path or (root / path).is_dir() and candidate.is_relative_to(root / path)
                                for path in driver_inputs)):
                raise ValueError("Include the local qualification driver in its check input_paths")
        if "input_digest" in check:
            if not isinstance(check["input_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", check["input_digest"]):
                raise ValueError("stored qualification input pin is malformed")
            normalized[-1]["input_digest"] = check["input_digest"]
    result = {"checks": normalized, **shared}
    if stored and "source_epoch" in payload:
        result["source_epoch"] = payload["source_epoch"]
    return result


def plan_declaration(plan):
    """Compare caller declarations without runner-owned pins or source provenance."""
    return {**{key: value for key, value in plan.items() if key not in {"checks", "source_epoch"}},
            "checks": [{key: value for key, value in check.items() if key != "input_digest"}
                       for check in plan["checks"]]}


def plan_digest(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def _full_npm_gate(check):
    """The bounded final-coverage surface: full scripts, never filtered tests."""
    argv = check['argv']
    prefix, assignments, index = None, [], 0
    if argv[0] in {'env', '/usr/bin/env'}:
        prefix, index = argv[0], 1
        while index < len(argv) and '=' in argv[index]:
            name, value = argv[index].split('=', 1)
            if not _ENV_NAME.fullmatch(name) or name in {n for n, _ in assignments}:
                raise ValueError('Final coverage requires unique literal environment assignments')
            assignments.append((name, value))
            index += 1
    command = argv[index:]
    if (len(command) < 3 or command[:2] != ['npm', 'run'] or command[2] not in {'check', 'test:e2e'}
            or check.get('cwd', '.') != '.' or any(k in check for k in ('input_paths', 'env_names', 'correction', 'input_digest'))):
        raise ValueError('Final coverage supports only the declared full npm check/test:e2e gates at the repository root')
    script, tail, config, workers = command[2], command[3:], None, None
    if tail:
        if script != 'test:e2e' or tail[0] != '--':
            raise ValueError('Final coverage cannot replace, filter or add arguments to a full gate')
        for flag in tail[1:]:
            if flag.startswith('--config=') and config is None:
                config = flag.removeprefix('--config=')
                if not config or Path(config).is_absolute() or '..' in Path(config).parts:
                    raise ValueError('The previous configuration must be a declared relative input')
            elif re.fullmatch(r'--workers=[1-9][0-9]*', flag) and workers is None:
                workers = int(flag.split('=', 1)[1])
            else:
                raise ValueError('Final coverage cannot replace, filter or add arguments to a full gate')
    return prefix, assignments, script, config, workers


def final_coverage(original, final, scope, payload):
    """Explain current full-gate coverage; never assert old private bytes or runs."""
    keys = {'original_plan_id', 'final_plan_id', 'scope_reason', 'checks', 'criteria', 'changes'}
    text = lambda value: isinstance(value, str) and bool(value.strip())
    if (not isinstance(payload, dict) or set(payload) != keys
            or payload['original_plan_id'] != original['plan_id'] or payload['final_plan_id'] != final['plan_id']
            or not text(payload['scope_reason'])):
        raise ValueError('Final coverage requires exact original/final plan IDs and an explicit scope explanation')
    old, new = original['plan'], final['plan']
    before = {c['id']: c for c in old['checks']}
    after = {c['id']: c for c in new['checks']}
    if (set(old) - {'checks', 'input_paths', 'env_names'} or set(new) - {'checks', 'input_paths', 'env_names'}
            or set(before) != set(after) or len(before) != len(old['checks']) or len(after) != len(new['checks'])
            or not isinstance(payload['checks'], dict) or set(payload['checks']) != set(before)
            or any(not text(reason) for reason in payload['checks'].values())):
        raise ValueError('Every original required check needs its own same-identity final full-gate coverage')
    criteria = scope.get('acceptance_criteria')
    if (not isinstance(criteria, list) or not criteria or not isinstance(payload['criteria'], list)
            or len(payload['criteria']) != len(criteria) or any(not text(reason) for reason in payload['criteria'])):
        raise ValueError('Explain coverage of every original acceptance criterion in its recorded order')
    differences, removed_configs, added_names, scripts = {}, set(), set(), {}
    def difference(path, previous, current):
        if previous != current:
            differences[path] = {'previous': previous, 'final': current}
    for identifier, previous in before.items():
        current = after[identifier]
        old_prefix, old_env, script, old_config, old_workers = _full_npm_gate(previous)
        prefix, environment, final_script, config, workers = _full_npm_gate(current)
        if (prefix != old_prefix or environment[:len(old_env)] != old_env or script != final_script or config is not None
                or (old_workers is not None and (workers is None or workers > old_workers))):
            raise ValueError('Preserve the runner, literal existing environment and full gate; only explicit native controls may differ')
        # These are the evidenced native execution controls for this bounded
        # route, not permission to infer harmlessness from an env-looking name.
        allowed = {'VITEST_MAX_WORKERS': None} if script == 'check' else {'POISE_E2E_PORT': 65535}
        for name, value in environment[len(old_env):]:
            if name not in allowed or not re.fullmatch(r'[1-9][0-9]*', value) or (allowed[name] and int(value) > allowed[name]):
                raise ValueError('Unsupported environment change in final full-gate coverage')
            added_names.add(name)
        if not any(re.search(r'(?<![\w:])' + re.escape('npm run ' + script) + r'(?![\w:])', criterion) for criterion in criteria):
            raise ValueError('The immutable original acceptance must explicitly require this full repository gate')
        if old_config:
            if old_config not in old.get('input_paths', []):
                raise ValueError('Retain the original private configuration declaration in the historical plan')
            removed_configs.add(old_config)
        scripts[identifier] = script
        for field in ('argv', 'reason', 'cwd'):
            difference(f'checks/{identifier}/{field}', previous.get(field, '.' if field == 'cwd' else None),
                       current.get(field, '.' if field == 'cwd' else None))
    if (set(new.get('input_paths', [])) != set(old.get('input_paths', [])) - removed_configs
            or not set(old.get('env_names', [])).issubset(new.get('env_names', []))
            or not (set(new.get('env_names', [])) - set(old.get('env_names', []))).issubset(added_names)):
        raise ValueError('Only a replaced private browser configuration and declared native controls may change input declarations')
    for field in ('input_paths', 'env_names'):
        difference(field, old.get(field, []), new.get(field, []))
    if (not isinstance(payload['changes'], dict) or set(payload['changes']) != set(differences)
            or any(not text(reason) for reason in payload['changes'].values())):
        raise ValueError('Explain every actual declaration difference; no undeclared or invented differences are accepted')
    return {'scope_reason': payload['scope_reason'], 'checks': payload['checks'], 'scripts': scripts,
            'criteria': [{'criterion': criterion, 'reason': reason} for criterion, reason in zip(criteria, payload['criteria'])],
            'declaration_changes': {path: {**values, 'reason': payload['changes'][path]} for path, values in differences.items()},
            'historical_input_continuity': 'not_established', 'historical_results_changed': False}


def _git(root: Path, *args: str, data=None) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], input=data, capture_output=True, timeout=10)
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


def source_snapshots(cwd, previous_head, final_head):
    """Read both published trees without trusting index hints or running filters."""
    if (any(not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head)
            for head in (previous_head, final_head)) or previous_head == final_head):
        raise ValueError("--from-head requires a distinct full previous source SHA")
    root = Path(cwd)
    _git(root, "--no-replace-objects", "merge-base", "--is-ancestor", previous_head, final_head)
    trees = []
    for head in (previous_head, final_head):
        files, directories = {}, set()
        for entry in _git(root, "--no-replace-objects", "ls-tree", "-r", "-t", "-l", "-z", "--full-tree", head).split(b"\x00"):
            if not entry:
                continue
            header, path = entry.split(b"\t", 1)
            mode, kind, blob, size = header.split()
            path = os.fsdecode(path)
            if mode == b"040000" and kind == b"tree":
                directories.add(path)
            elif mode in {b"100644", b"100755", b"120000"} and kind == b"blob":
                files[path] = (int(mode, 8), blob, int(size))
            else:
                raise ValueError("A final-head epoch requires reconstructable ordinary Git source files")
        trees.append((files, directories))
    old, final = trees[0][0], trees[1][0]
    for path in old.keys() | final.keys():
        if (any(entry and stat.S_ISLNK(entry[0]) for entry in (old.get(path), final.get(path)))
                and old.get(path) != final.get(path)):
            raise ValueError("A final-head epoch requires reconstructable ordinary Git source files")
        if (path not in final and path not in trees[1][1]
                and ((root / path).exists() or (root / path).is_symlink())):
            raise ValueError("Removed published source is still present in the checkout")
    for path in trees[0][1] | trees[1][1]:
        if (root / path).is_symlink():
            raise ValueError("Published source directories cannot be symlinked")
    paths = old.keys() | final.keys() | trees[0][1] | trees[1][1]
    # Only historical directory types replace current traversal. In the final
    # checkout a directory may remain solely for unchanged private inputs.
    return tuple(({path: files.get(path) for path in paths}, directories if index == 0 else None)
                 for index, (files, directories) in enumerate(trees))


def _source_blobs(root, files):
    """Fetch bounded blob groups instead of retaining both complete source trees."""
    def read(batch):
        stream = _git(root, "--no-replace-objects", "cat-file", "--batch",
                      data=b"\n".join(entry[2] for entry in batch) + b"\n")
        position = 0
        for path, mode, expected, expected_size in batch:
            end = stream.index(b"\n", position)
            actual, kind, size = stream[position:end].split()
            size = int(size)
            position = end + 1
            if (actual != expected or kind != b"blob" or size != expected_size
                    or stream[position + size:position + size + 1] != b"\n"):
                raise ValueError("Historical Git source could not be reconstructed")
            yield path, mode, memoryview(stream)[position:position + size]
            position += size + 1
    batch, total = [], 0
    for path in sorted(files):
        if files[path] is None:
            continue
        mode, blob, size = files[path]
        if batch and (len(batch) == 128 or total + size > _SOURCE_BLOB_BATCH_BYTES):
            yield from read(batch)
            batch, total = [], 0
        batch.append((path, mode, blob, size))
        total += size
    if batch:
        yield from read(batch)


def fingerprint(cwd: str | Path, plan: dict, *, source_snapshot=None) -> str:
    """Hash current source, not HEAD, timestamps, caches, or test output.

    Every tracked and untracked nonignored repository file is included. Commit
    bookkeeping with identical content preserves evidence. A check can include
    otherwise ignored inputs explicitly. The caller must compare this digest
    before and after waiting for the actual process exit.
    """
    working = Path(cwd).expanduser().resolve()
    normalized = normalize_plan(plan, working, stored=True)
    repository = Path(repo_root(working))
    paths = {os.fsdecode(path) for path in _git(repository, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\x00") if path}
    source_files, source_directories = source_snapshot if source_snapshot is not None else ({}, None)
    source_contents = iter(_source_blobs(repository, source_files))
    paths.update(source_files)
    explicit = set()
    for value in normalized["input_paths"]:
        candidate = working / value
        relative = candidate.relative_to(repository).as_posix()
        directory = candidate.is_dir() and not candidate.is_symlink()
        input_files = set()
        if directory:
            for current, folders, files in os.walk(candidate, followlinks=False):
                folders[:] = [folder for folder in folders if folder != ".git"]
                for name in files + [folder for folder in folders if (Path(current) / folder).is_symlink()]:
                    child = (Path(current) / name).relative_to(repository)
                    # Vitest regenerates execution timings inside its dependency tree.
                    # Tracked files and individually declared inputs still take precedence.
                    if (len(child.parts) >= 5 and child.parts[-5:-2] == ("node_modules", ".vite", "vitest")
                            and re.fullmatch(r"[0-9a-f]{40}", child.parts[-2]) and name == "results.json"):
                        continue
                    input_files.add(child.as_posix())
        # Never omit current private contents while reconstructing a historical
        # file/missing path. The old pin must prove those contents unchanged.
        paths.update(input_files)
        if source_directories is not None and relative in source_files:
            directory = (relative in source_directories or source_files[relative] is None
                         and bool(input_files - source_files.keys()))
        if not directory:
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
        if value in source_files:
            historical = source_files[value]
            if historical is None:
                if value in explicit:
                    append(os.fsencode(value))
                    append(b"missing")
                continue
            source_path, mode, body = next(source_contents)
            if source_path != value:
                raise ValueError("Published source paths changed during reconstruction")
            append(os.fsencode(value))
            if stat.S_ISLNK(mode):
                # Unchanged links retain the existing target-content pin. The
                # published link text is independent of the current index.
                append(str(stat.S_IMODE(path.lstat().st_mode)).encode())
                append(b"symlink")
                append(body)
                if path.is_file():
                    append(path.read_bytes())
                elif path.exists():
                    raise ValueError("directory symlink inputs require explicit file inputs")
            else:
                append(str(stat.S_IMODE(mode)).encode())
                append(b"file")
                append(body)
            continue
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


def check_fingerprint(cwd, plan, check, *, source_snapshot=None):
    """Bind a control's added inputs without changing retained check identities."""
    scoped = {"checks": [{key: check[key] for key in ("id", "argv", "reason", "cwd") if key in check}]}
    for name in ("input_paths", "env_names"):
        scoped[name] = sorted(set(plan.get(name, [])) | set(check.get(name, [])))
    return fingerprint(cwd, scoped, source_snapshot=source_snapshot)


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
                      local_state: dict | None = None, preflight: dict | None = None, *, source_closed=False,
                      check_digests: dict | None = None) -> dict:
    """Order validation, publication and proven review conclusions.

    A run may pass only after its managed process actually exited successfully
    on unchanged before/after inputs. Remote merge state is never completion
    proof. Missing or stale remote evidence stays WAIT_REMOTE.
    """
    corrected_failures, corrected_unexecuted = [], []
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
                "reason": reason, **({"corrected_failures": corrected_failures} if corrected_failures else {}),
                **({"corrected_unexecuted": corrected_unexecuted} if corrected_unexecuted else {}), **fields}
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
    # A full collection proves the same ancestry and more. It supersedes an
    # older preflight; a newer failed preflight must still invalidate old proof.
    preflight_boundary=(preflight or {}).get('attempted_at',(preflight or {}).get('collected_at'))
    full_stamp=(snapshot or {}).get('collected_at')
    full_newer=(isinstance(full_stamp,(int,float)) and isinstance(preflight_boundary,(int,float))
        and not isinstance(full_stamp,bool) and not isinstance(preflight_boundary,bool) and full_stamp>=preflight_boundary)
    validation_evidence=snapshot if preflight is None or full_newer else preflight
    stamp=(validation_evidence or {}).get('collected_at')
    if not isinstance(stamp,(int,float)) or isinstance(stamp,bool) or not 0<=time.time()-stamp<=REMOTE_EVIDENCE_TTL:
        return result('REMOTE_READ_REQUIRED', 'Verify the remote PR head before running local checks')
    if validation_evidence.get('head_sha') != head:
        return result('PUBLISH_REQUIRED', 'Push the current commit now; local checks must not delay publication')
    from .policy import branch_freshness_gate
    freshness = branch_freshness_gate(validation_evidence)
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
    expected = {check["id"]: check.get("input_digest", current_digest) for check in plan["checks"]}
    changed = [check["id"] for check in plan["checks"] if "input_digest" in check
               and (check_digests or {}).get(check["id"]) != check["input_digest"]]
    if changed:
        return result("INPUTS_CHANGED", "Frozen qualification control inputs changed; retain their original pins and results", changed_checks=changed)
    if (any("correction" in check for check in plan["checks"])
            and (validation_evidence.get("state") != "closed" or validation_evidence.get("merged") is not True)):
        return result("REMOTE_READ_REQUIRED", "Checker corrections require current proof of the retained merged source")
    latest = {}
    active = set()
    for run in runs:
        if run.get("check_id") not in checks or run.get("input_digest") != expected[run["check_id"]]:
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
              and run["exit_code"] == 0 and run.get("before_digest") == expected[identifier]
              and run.get("after_digest") == expected[identifier]):
            passed.append(identifier)
        elif run.get("status") == "fail":
            failed.append(identifier)
        else:
            missing.append(identifier)
    # A correction never rewrites a run or labels its predecessor PASS.
    # Follow only admitted, nonbranching links to an actual passing successor;
    # every unexecuted, failed or unverifiable end of the chain stays required.
    coverage = {identifier: latest[identifier] for identifier in passed}
    executed = {run.get("check_id") for run in runs}
    for _ in checks:
        extended = False
        for check in plan["checks"]:
            correction = check.get("correction")
            if not correction or check["id"] not in coverage:
                continue
            target = correction.get("check_id")
            previous = latest.get(target)
            if target not in checks or target in coverage:
                continue
            unexecuted = correction.get("unexecuted") is True
            if unexecuted:
                if target in executed:
                    continue
            elif (not previous or previous.get("run_id") != correction.get("run_id")
                    or previous.get("status") != "fail" or type(previous.get("exit_code")) is not int
                    or previous["exit_code"] <= 0 or previous.get("before_digest") != expected[target]
                    or previous.get("after_digest") != expected[target]):
                continue
            proof = coverage[check["id"]]
            coverage[target] = proof
            receipt = {"check_id": target, "corrected_by": check["id"],
                "passed_check_id": proof["check_id"], "passed_run_id": proof["run_id"]}
            if unexecuted:
                corrected_unexecuted.append({**receipt, "unexecuted": True})
            else:
                corrected_failures.append({**receipt, "failed_run_id": previous["run_id"], "exit_code": previous["exit_code"]})
            extended = True
        if not extended:
            break
    failed = [identifier for identifier in failed if identifier not in coverage]
    missing = [identifier for identifier in missing if identifier not in coverage]
    if failed:
        return result("VALIDATE_FAILED", "Fix the actual failed check, push the fix promptly, then rerun its required validation", failed=failed, pending=sorted(failed + missing), passed=passed, running=sorted(active))
    if missing:
        return result("VALIDATE_REQUIRED", "Run the distinct missing necessary validations", pending=missing, passed=passed, running=sorted(active))
    if active:
        return result("VALIDATE_RUNNING", "Managed checks are running; work on the next authorized PR and let the monitor check remote state", running=sorted(active), pending=[], passed=passed)
    if source_closed or snapshot.get('state') == 'closed' or validation_evidence.get('state') == 'closed':
        return result('WAIT_REMOTE', 'Retained local checks passed; PR closure does not establish task acceptance',
                      next_action='Report the retained local results against the original task and verify its remaining acceptance; do not repeat passed checks.',
                      local_checks_passed=True, completion_verified=False, source_closed=True, passed=passed)
    if not fresh_remote() or (preflight is not None and (not full_newer and snapshot.get("collected_at",0)<(preflight.get("collected_at") or 0))):
        return result("WAIT_REMOTE", "Local preflight proves only head and ancestry; collect full fresh CI/review/closure evidence for completion")
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

