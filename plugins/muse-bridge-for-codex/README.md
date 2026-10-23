# Muse control bridge for Codex

Local plugin and MCP server for launching, reading, messaging, steering, interrupting, and resuming Muse sessions.

**Fixed route:** `meta` / `muse-spark-1.3-contributor`, contributor mode on, effort `max`.
Every provider request passes an HTTP guard before reaching Meta. Other models, missing/lower efforts, route overrides, and redirects are refused. There is no fallback.
The guard also blocks Muse's built-in helpers when they request a lower effort; those helper features then fail instead of using another tier.

Requires Python 3.11+, Codex CLI, and authenticated Muse 1.4.11 with the verified MSP schema. A changed Muse protocol stops startup until reverified.
Install tools: `python3 install.py`. Add the selector provider: `python3 install.py --provider`, then restart Codex.
Both install modes refuse while the persistent controller holds its lifetime lock, before changing runtime files, the CLI wrapper or Codex settings. Let managed sessions finish, stop that controller, then retry; installation never stops live Muse work automatically. The lock stays held through publication and provider service preparation, and is released before the new daemon starts. A successful reinstall uses the new runtime on its next controller start, rather than silently keeping old code in memory.
Check health: `muse-control call muse_status`.
Run offline checks: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests`.

The MCP tools provide `muse_launch`, `muse_list`, `muse_read`, `muse_send`, `muse_steer`, `muse_interrupt`, `muse_resume`, `muse_events`, `muse_pending`, and `muse_approve`.
Agent messages stream through `muse_events`; retained transcript pages come from `muse_read`.
State and the private Unix control socket live under `~/.local/state/muse-control`; the controller starts on demand and persists across MCP client disconnects.
Use `muse_resume` after a controller restart. An existing CLI must release its session lease before this host can resume it.

The policy covers bridge-managed provider requests. Separately launched processes and the provider's internal implementation are outside that guarantee.
The provider installer selects the Muse catalog globally and backs up the prior configuration privately. The current Codex picker uses one provider catalog at a time.
A private loopback gateway retains native Muse authentication in RAM; a pinned max-effort READY turn refreshes it. No Meta token is exported to files or Codex.
Canonical source: `mikkokotila/agent-tooling/plugins/muse-bridge-for-codex`.
