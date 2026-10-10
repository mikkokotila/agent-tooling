#!/bin/sh
export PYTHONPATH=${HOME}/.local/share/caller-pr-gates
export CODEX_CLI=/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex
export CLAUDE_CLI=${HOME}/.local/bin/claude
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${HOME}/.local/bin
exec ${HOME}/.local/share/caller-pr-stop-gate/bin/python -P -m caller_pr_gates.cli "$@"
