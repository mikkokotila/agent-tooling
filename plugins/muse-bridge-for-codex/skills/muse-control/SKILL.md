---
name: muse-control
description: Launch, read, message, steer, interrupt, and resume local Muse agents with the contributor model and max effort enforced.
---

Use the muse_control MCP tools. If tools have not loaded in this chat, the same
running controller is reachable with:

`muse-control call TOOL --args 'JSON'`

Read `muse_status` before launching work. The only permitted inference route is
Meta `muse-spark-1.3-contributor`, contributor mode on, effort `max`.
The bridge has no model/effort options, arbitrary RPC, or fallback route.
Never bypass its provider guard or use an unguarded Muse invocation as a fallback.
An unavailable contributor model, model mismatch, changed protocol, or rejected
lower-effort request must remain a truthful failure.

Start agents with `muse_launch` and an existing absolute workspace. Collect
their session IDs. `muse_send` queues the next prompt; `muse_steer` targets an
exact active turn. Use `muse_read` for retained transcripts and `muse_events`
for live replies; preserve pagination and event cursors. `muse_interrupt` stops
work. `muse_resume` loads a stopped contributor session and enforces max before
admitting new prompts. A foreign running CLI lease must be released by its owner;
the bridge never silently takes it over. Other model sessions are refused.

Use `muse_pending` and `muse_approve` for explicit approval decisions within the
user's authorized task. A presentation receipt does not approve an action.
Sending messages to people or unrelated chats still requires user authorization.

For an uncertain write result, inspect the session and reuse the same UUIDv7
`command_id` when retrying. Do not blindly submit a new prompt.

The enforcement guarantee covers requests passing through this controller.
Independent Muse invocations or separately configured providers are outside it.
Never claim all machine-wide processes or the provider's internal implementation
are controlled. The provider guard rejects low/minimal helper inference too;
those helpers may be unavailable rather than silently using a lower effort.
