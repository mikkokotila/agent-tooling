import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from caller_pr_gates.progress import evaluate_progress, fingerprint, literal_exec_commands, normalize_plan, plan_digest, validation_command, validation_decision, working_state
from test_policy import fresh_branch


class ValidationProgressRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.root / "source.py").write_text("x = 1\n")
        (self.root / ".gitignore").write_text("cache/\nignored.json\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.plan = normalize_plan({"checks": [
            {"id": "unit", "argv": ["python", "-m", "unittest"], "reason": "Regression coverage"},
            {"id": "lint", "argv": ["ruff", "check", "."], "reason": "Repository-required lint"},
        ]}, self.root)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def success(self, check="unit", digest="same"):
        return dict(check_id=check, input_digest=digest, status="pass", exit_code=0,
                    before_digest=digest, after_digest=digest)

    def test_commit_only_change_preserves_passes(self):
        before = fingerprint(self.root, self.plan)
        self.git("commit", "--allow-empty", "-qm", "metadata only")
        self.assertEqual(before, fingerprint(self.root, self.plan))

    def test_commit_of_previously_validated_content_preserves_passes(self):
        (self.root / "source.py").write_text("x = 2\n")
        before = fingerprint(self.root, self.plan)
        self.git("add", "source.py")
        self.git("commit", "-qm", "real fix")
        self.assertEqual(before, fingerprint(self.root, self.plan))

    def test_commit_of_previously_validated_deletion_preserves_passes(self):
        (self.root / "source.py").unlink()
        before = fingerprint(self.root, self.plan)
        self.git("add", "source.py")
        self.git("commit", "-qm", "delete obsolete source")
        self.assertEqual(before, fingerprint(self.root, self.plan))

    def test_source_and_untracked_input_changes_invalidate_passes(self):
        before = fingerprint(self.root, self.plan)
        (self.root / "new_test.py").write_text("assert True\n")
        self.assertNotEqual(before, fingerprint(self.root, self.plan))
        (self.root / "new_test.py").unlink()
        self.assertEqual(before, fingerprint(self.root, self.plan))
        (self.root / "source.py").write_text("x = 2\n")
        self.assertNotEqual(before, fingerprint(self.root, self.plan))

    def test_test_output_cache_does_not_authorize_reassurance(self):
        before = fingerprint(self.root, self.plan)
        (self.root / "cache").mkdir()
        (self.root / "cache" / "last-result").write_text("passed")
        self.assertEqual(before, fingerprint(self.root, self.plan))

    def test_explicit_ignored_inputs_are_hashed(self):
        plan = {**self.plan, "input_paths": ["ignored.json"]}
        before = fingerprint(self.root, plan)
        (self.root / "ignored.json").write_text('{"seed": 2}')
        self.assertNotEqual(before, fingerprint(self.root, plan))

    def test_dependency_directory_ignores_generated_vitest_timings_but_hashes_code(self):
        (self.root / ".gitignore").write_text("node_modules/\n")
        package = self.root / "node_modules" / "example" / "index.js"
        package.parent.mkdir(parents=True)
        package.write_text("export const value = 1\n")
        cache = self.root / "node_modules" / ".vite" / "vitest" / ("a" * 40) / "results.json"
        plan = {**self.plan, "input_paths": ["node_modules"]}
        before = fingerprint(self.root, plan)
        cache.parent.mkdir(parents=True)
        cache.write_text('{"results": []}')
        self.assertEqual(before, fingerprint(self.root, plan))
        cache.write_text('{"results": [["test", {"duration": 42}]]}')
        self.assertEqual(before, fingerprint(self.root, plan))
        package.write_text("export const value = 2\n")
        self.assertNotEqual(before, fingerprint(self.root, plan))

    def test_explicit_and_tracked_vitest_cache_files_remain_inputs(self):
        (self.root / ".gitignore").write_text("node_modules/\n")
        cache = self.root / "node_modules" / ".vite" / "vitest" / ("a" * 40) / "results.json"
        cache.parent.mkdir(parents=True)
        cache.write_text("one")
        plan = {**self.plan, "input_paths": ["node_modules", cache.relative_to(self.root).as_posix()]}
        before = fingerprint(self.root, plan)
        cache.write_text("two")
        self.assertNotEqual(before, fingerprint(self.root, plan))
        self.git("add", "--force", "node_modules")
        directory_plan = {**self.plan, "input_paths": ["node_modules"]}
        before = fingerprint(self.root, directory_plan)
        cache.write_text("three")
        self.assertNotEqual(before, fingerprint(self.root, directory_plan))

    def test_symlink_target_changes_are_hashed(self):
        (self.root / "ignored.json").write_text("one")
        (self.root / "linked.json").symlink_to("ignored.json")
        before = fingerprint(self.root, self.plan)
        (self.root / "ignored.json").write_text("two")
        self.assertNotEqual(before, fingerprint(self.root, self.plan))

    def test_distinct_required_validation_remains_allowed(self):
        decision = validation_decision(self.plan, [self.success()], "same", "lint")
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["phase"], "VALIDATE")

    def test_successful_repeat_is_denied(self):
        self.assertFalse(validation_decision(self.plan, [self.success()], "same", "unit")["allowed"])

    def test_all_checks_passed_requires_publication(self):
        decision = validation_decision(self.plan, [self.success(), self.success("lint")], "same")
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["phase"], "PUBLISH_REQUIRED")

    def test_failure_can_be_retried_without_forcing_publication(self):
        failed = {**self.success(), "status": "fail", "exit_code": 1}
        decision = validation_decision(self.plan, [failed], "same", "unit")
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["phase"], "VALIDATE")

    def test_changed_inputs_allow_revalidation(self):
        self.assertTrue(validation_decision(self.plan, [self.success()], "changed", "unit")["allowed"])

    def test_running_validation_cannot_pass_or_duplicate(self):
        running = {**self.success(), "status": "running", "exit_code": None}
        self.assertFalse(validation_decision(self.plan, [running], "same", "unit")["allowed"])
        self.assertTrue(validation_decision(self.plan, [running], "same", "lint")["allowed"])

    def test_changed_inputs_during_process_do_not_pass(self):
        stale = {**self.success(), "after_digest": "changed"}
        self.assertTrue(validation_decision(self.plan, [stale], "same", "unit")["allowed"])

    def test_tool_yield_has_no_pass_evidence(self):
        yielded = {**self.success(), "exit_code": None}
        self.assertTrue(validation_decision(self.plan, [yielded], "same", "unit")["allowed"])

    def test_plan_rejects_empty_duplicate_and_escaped_workspaces(self):
        for payload in [{"checks": []}, {"checks": [self.plan["checks"][0]] * 2},
                        {"checks": [{**self.plan["checks"][0], "cwd": ".."}]},
                        {**self.plan, "input_paths": ["../secret"]}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize_plan(payload, self.root)

    def test_changed_command_plan_cannot_manufacture_changed_source(self):
        before = fingerprint(self.root, self.plan)
        changed = {"checks": [{**self.plan["checks"][0], "argv": ["python", "-m", "unittest", "-v"]}]}
        self.assertEqual(before, fingerprint(self.root, changed))
        self.assertNotEqual(plan_digest(self.plan), plan_digest(normalize_plan(changed, self.root)))

    def test_plan_rejects_masked_shell_or_inline_success(self):
        for argv in [["sh", "-c", "pytest; true"], ["bash", "-c", "pytest || true"],
                     ["true"], ["echo", "passed"], ["python", "-c", "raise SystemExit(0)"],
                     ["node", "-e", "process.exit(0)"], ["pytest", ";", "true"],
                     ["pytest -q && true"]]:
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                normalize_plan({"checks": [{"id": "x", "argv": argv, "reason": "Check regression"}]}, self.root)

    def test_actual_repository_script_is_allowed(self):
        script = self.root / "check_project.py"
        script.write_text("import unittest\n")
        normalized = normalize_plan({"checks": [{"id": "x", "argv": ["python", "check_project.py"], "reason": "Project-specific regression suite"}]}, self.root)
        self.assertEqual(normalized["checks"][0]["argv"], ["python", "check_project.py"])

    def test_selected_environment_change_invalidates_actual_inputs(self):
        plan = {**self.plan, "env_names": ["CALLER_TEST_SEED"]}
        with patch.dict(os.environ, {"CALLER_TEST_SEED": "one"}):
            before = fingerprint(self.root, plan)
        with patch.dict(os.environ, {"CALLER_TEST_SEED": "two"}):
            self.assertNotEqual(before, fingerprint(self.root, plan))

    def test_command_hint_detects_executed_checks_not_heredoc_data(self):
        for command in ["pytest -q", "uv run python -m unittest", "cd repo && npm run typecheck", "cargo test", "make verify"]:
            with self.subTest(command=command):
                self.assertTrue(validation_command(command))
        self.assertFalse(validation_command("cat <<'DATA'\npytest -q\nDATA"))
        self.assertFalse(validation_command("git status --short"))
        self.assertFalse(validation_command('echo "pytest -q"'))
        self.assertFalse(validation_command('rg "pytest" source.py'))
        self.assertFalse(validation_command('python -m json.tool pytest'))

    def test_multiline_shell_checks_use_executable_positions(self):
        self.assertTrue(validation_command('echo status\npytest -q'))
        self.assertFalse(validation_command('echo "status\npytest -q"'))
        self.assertTrue(validation_command('./scripts/test.sh'))
        self.assertTrue(validation_command('python scripts/check_project.py'))

    def test_literal_functions_exec_nested_commands_are_detected(self):
        for source in ['await tools.exec_command({cmd: "pytest -q", workdir: "/repo"});',
                       "const result = await tools.exec_command({'cmd':'python -m pytest tests', 'workdir':'/repo'}); text(result);",
                       'await tools.exec_command({cmd: `ruff check .`, workdir: `/repo`});']:
            with self.subTest(source=source):
                self.assertTrue(validation_command(source))
                self.assertEqual(literal_exec_commands(source)[0]['cwd'], '/repo')

    def test_js_literal_data_comments_and_dynamic_commands_are_not_guessed(self):
        for source in ['const note = "tools.exec_command({cmd: \'pytest\'})"; text(note);',
                       'const source = `await tools.exec_command({cmd: "pytest"})`;',
                       '// tools.exec_command({cmd: "pytest"})\ntext("done");',
                       '/* tools.exec_command({cmd: "pytest"}) */ text("done");',
                       'await tools.exec_command({cmd: commandVariable});',
                       'await tools.exec_command({cmd: `pytest ${arguments}`});',
                       'await tools.exec_command({cmd: "git status", note: "pytest"});']:
            with self.subTest(source=source):
                self.assertFalse(validation_command(source))

    def test_nested_exec_file_write_heredoc_is_data(self):
        source = 'await tools.exec_command({cmd: "cat <<\'JSON\'\\n{\\\"cmd\\\":\\\"pytest -q\\\"}\\nJSON", workdir: "/repo"});'
        self.assertFalse(validation_command(source))
        direct = 'cat <<\'JS\'\nawait tools.exec_command({cmd: "pytest -q"});\nJS'
        self.assertFalse(validation_command(direct))

    def test_working_state_includes_untracked_work(self):
        self.assertTrue(working_state(self.root)["clean"])
        (self.root / "unpublished.py").write_text("new = 1\n")
        self.assertFalse(working_state(self.root)["clean"])

    def phase(self, **changes):
        row = {"plan_id": "p", "cwd": str(self.root), "input_digest": "same", "plan": self.plan}
        snapshot = {"head_sha": "head", "head_sha_after": "head", "collected_at": time.time(), **fresh_branch('head')}
        gates = [{"gate": name, "status": "PASS", "evidence": {"obligations": []}} for name in ["PR_NOT_DRAFT", "CI_FAILED", "CI_PENDING", "REVIEW_HANDLED", "REVIEW_CONCLUDED", "MERGE_CONFLICT"]]
        args = {"row": row, "runs": [self.success(), self.success("lint")], "current_digest": "same", "snapshot": snapshot,
                "review_gates": gates, "local_state": {"head": "head", "clean": True}}
        args.update(changes)
        return evaluate_progress(**args)

    def test_all_local_passes_dirty_work_requires_publish_not_retest(self):
        self.assertEqual(self.phase(local_state={"head": "head", "clean": False})["phase"], "PUBLISH_REQUIRED")

    def test_actual_exit_and_unchanged_after_inputs_required(self):
        for run in [{**self.success("lint"), "exit_code": None}, {**self.success("lint"), "after_digest": "changed"}]:
            self.assertEqual(self.phase(runs=[self.success(), run])["phase"], "VALIDATE_REQUIRED")

    def test_conclusion_precedes_even_new_local_inputs(self):
        gates = [{"gate": "REVIEW_HANDLED", "status": "PASS", "evidence": {"obligations": [{"id": "inline:1", "handled": True, "concluded": False}]}}]
        result = self.phase(current_digest="changed", review_gates=gates)
        self.assertEqual(result["phase"], "CONCLUDE_REQUIRED")
        self.assertTrue(result["blocked"])
        self.assertIn("Reply", result["next_action"])

    def test_unhandled_comments_cannot_be_falsely_resolved(self):
        gates = [{"gate": "REVIEW_HANDLED", "status": "FAIL", "evidence": {"obligations": [{"id": "inline:1", "handled": False, "concluded": False}]}}]
        self.assertEqual(self.phase(review_gates=gates)["phase"], "ADDRESS_REQUIRED")

    def test_stale_unconcluded_comments_require_remote_read_before_more_tests(self):
        gates = [{"gate": "REVIEW_HANDLED", "status": "PASS", "evidence": {"obligations": [{"id": "inline:1", "handled": True, "concluded": False}]}}]
        snapshot = {"head_sha": "head", "head_sha_after": "head", "collected_at": time.time() - 500}
        self.assertEqual(self.phase(snapshot=snapshot, runs=[], review_gates=gates)["phase"], "REMOTE_READ_REQUIRED")

    def test_distinct_necessary_run_can_start_while_another_is_running(self):
        result = self.phase(runs=[{**self.success(), "status": "running", "exit_code": None}])
        self.assertEqual(result["phase"], "VALIDATE_REQUIRED")
        self.assertEqual(result["pending"], ["lint"])
        self.assertEqual(result["running"], ["unit"])

    def test_stale_snapshot_requires_live_remote_read(self):
        snapshot = {"head_sha": "head", "head_sha_after": "head", "collected_at": time.time() - 500}
        self.assertEqual(self.phase(snapshot=snapshot)["phase"], "REMOTE_READ_REQUIRED")

    def test_pending_remote_ci_does_not_reset_successful_local_validation(self):
        gates = [{"gate": "CI_PENDING", "status": "WAIT"}]
        self.assertEqual(self.phase(review_gates=gates)["phase"], "WAIT_REMOTE")

    def test_merge_is_not_completion_proof(self):
        snapshot = {"head_sha": "head", "head_sha_after": "head", "collected_at": time.time(), "merged": True, **fresh_branch('head')}
        self.assertEqual(self.phase(snapshot=snapshot, review_gates=[])["phase"], "WAIT_REMOTE")

    def test_current_published_head_and_all_gates_complete(self):
        result = self.phase()
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertFalse(result["blocked"])


if __name__ == "__main__":
    unittest.main()
