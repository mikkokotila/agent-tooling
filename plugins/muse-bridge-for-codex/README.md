# Muse control bridge for Codex

Local plugin and MCP server for launching, reading, messaging, steering, interrupting, and resuming Muse sessions.

**Fixed route:** `meta` / `muse-spark-1.3-contributor`, contributor mode on, effort `max`.
Every provider request passes an HTTP guard before reaching Meta. Other models, missing/lower efforts, route overrides, and redirects are refused. There is no fallback.
The guard also blocks Muse's built-in helpers when they request a lower effort; those helper features then fail instead of using another tier.

Requires Python 3.10+, Codex CLI, and authenticated Muse 1.4.11 with the verified MSP schema. A changed Muse protocol stops startup until reverified.
Install from the canonical plugin checkout: `python3 install.py`.
Check health: `muse-control call muse_status`.
Run offline checks: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests`.

The MCP tools provide `muse_launch`, `muse_list`, `muse_read`, `muse_send`, `muse_steer`, `muse_interrupt`, `muse_resume`, `muse_events`, `muse_pending`, and `muse_approve`.
Agent messages stream through `muse_events`; retained transcript pages come from `muse_read`.
State and the private Unix control socket live under `~/.local/state/muse-control`; the controller starts on demand and persists across MCP client disconnects.
Use `muse_resume` after a controller restart. An existing CLI must release its session lease before this host can resume it.

The policy covers bridge-managed provider requests. Separately launched processes and the provider's internal implementation are outside that guarantee.
Canonical source: `mikkokotila/agent-tooling/plugins/muse-bridge-for-codex`.
