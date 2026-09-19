# ADR 0002 — Separate the local device runtime before remote access

Status: implemented on the agentic pivot branch. Supersedes the in-process-only
device boundary in ADR 0001; this is not a cloud release.

[ADR 0003](0003-paired-local-tls.md) subsequently adds a tested optional local
mutual-TLS harness, enrollment/revocation and durable encrypted receipts. This
ADR retains the original stdio contract and its historical verification.

## Why this step

The first pivot registered a device but called its OS handlers inside Core.
That proved tool selection and permissions, not a device connection. The next
increment introduces an actual process boundary while preserving the text CLI,
Luna/Sol adapters, identity and local memory format.

```text
Terminal -> Luna -> Core (schemas, approval, action receipts)
                        |
                        | private stdio, versioned JSON commands/results
                        v
                  Device Worker -> allowlisted Windows integrations
                        |
                   observed result -> Core -> Luna -> terminal
```

## Implemented contracts

- `DeviceTransport`: device metadata, execute, refresh, close. Core routes by
  exact registered device ID to an attached transport, never an arbitrary
  online device. `attach_transport` is trusted host wiring, not a model tool.
- `StdioDeviceAgent`: an owned, hidden child process launched with isolated
  Python. Default for the new CLI; `EPIS_DEVICE_TRANSPORT=inprocess` explicitly
  restores the old local path. Unknown modes fail; no automatic fallback.
- `DeviceWorker`: builds its own tool registry, reports platform capabilities,
  checks schema/target/permission again and dispatches OS integrations. It
  imports no model client, MemoryManager, sensor collector or dotenv loader.
- Protocol v1: command ID, exact device, capability, arguments, Core approval
  flag and deadline. The model never constructs this transport envelope.
  Frames are bounded to 64 KiB, requests serialized, responses correlated,
  and calls have a 15-second transport timeout. No automatic reconnect/retry.
- Core exposes `get_devices` and `get_task_status` to Luna. `/devices`, `/tasks`
  and `/tools` offer direct terminal inspection without a model request.

The worker receives a small OS environment allowlist, not parent API keys or
Python injection settings. The private pipe handles provide the local channel;
there is no listening port, shared network token, public endpoint or firewall
change. This is **not an OS sandbox**: the worker has the same user privileges
and filesystem access as its parent. Separation limits accidental coupling,
not compromise by malicious code running as the same Windows user.

## Permissions and action lifecycle

Green tools are automatic. Yellow/red or explicitly confirmation-required
tools need a terminal approval bound to a copied call, displayed arguments
and target. Approval expires after 120 seconds; new requests cancel stale
approval. Device policy independently rejects an unapproved sensitive call.
For private local pipes, the worker trusts the Core's approval flag. That flag
alone is **not sufficient authentication for a future network transport**.

The local `agent_tasks.db` stores only UUID, tool name, device ID, state and
timestamp. It contains neither arguments, prompts, URLs, nor result bodies.
It is an unencrypted low-sensitivity operational journal, not a replacement
for encrypted personal memory. A file lease permits one owning CLI per journal.

```text
awaiting_confirmation -> running -> succeeded / failed / unknown
          |
          +-> cancelled (reject, expiry, quit, or restart)
```

Green actions pass immediately through the same journal claim. Core commits
`running` before dispatch. A request ID can be claimed once. On restart,
unfinished `running` receipts become `unknown`, never queued for replay.
Within a worker session, identical command IDs return cached results; a
changed payload using the same ID is rejected. The 4096-receipt limit fails
closed instead of evicting IDs and enabling replays. Restarting is explicit.
This is **not a distributed exactly-once guarantee**.

A transport failure may occur after the OS acted. Core records `unknown`,
stops further tool execution in that turn (including model-initiated retries),
and returns the uncertainty to Luna. An explicit new user request starts a
new turn; the system does not infer approval to repeat old work.

Closing the CLI terminates only its owned device helper. `close_app` continues
to send normal WM_CLOSE after approval; it does not kill user applications.

## Capabilities in this increment

The device registry now contains nine Windows tools: the original five plus
`get_battery`, `media_next`, `media_previous`, and confirmation-required
`open_url`. URL opening accepts only HTTPS without embedded credentials,
whitespace, backslashes or nonstandard ports. It requests navigation in the
default browser and does not claim the page loaded or that the URL is safe.
The user must approve the exact URL. No arbitrary URL scheme or shell exists.

Media actions remain global Windows signals, not Spotify-specific playback
control. Named song selection, arbitrary app automation, filesystem access,
notification delivery and real Codex task control are not implemented.
No hypothetical capability is advertised as operational.

## Verification and limitations

- 48 tests passed, including real Windows worker creation, read-only system/
  battery round trips, Core -> worker -> Core with a deterministic model stub,
  on-device approval rejection, process cleanup and timeout handling.
- Unit coverage includes replay/payload conflicts, expired commands/approval,
  journal recovery/single-owner locking, secret environment omission, exact
  multi-transport routing, URL validation and mocked media adapters.
- Windows PowerShell 5.1 startup, local inspection commands and quit passed.
- Actual volume round trip through the separate worker passed at the current
  level; the original exact volume was restored and the worker exited.
- After explicit user authorization for device/task metadata transmission,
  all seven live OpenAI smoke cases passed on 2026-09-19: greeting, system
  information through the worker, device listing, battery through the worker,
  action receipts, unsupported song without unrelated playback, and
  Luna -> Sol -> Luna synthesis. The test blocked OS mutations and private
  context retrieval and did not persist test conversations or action receipts.
  This verifies the exercised paths, not every possible model decision.
- Process liveness is local availability, not a remote heartbeat or Windows
  service health check. No Windows startup installation has been performed.

## Next migration, not implemented here

1. Device enrollment and authenticated outbound TLS transport with heartbeat,
   revocation, policy-scoped authority and durable remote deduplication.
2. Cloud Core with low-sensitivity routing/task metadata; private retrieval
   stays behind a separate consent/minimization contract on the local device.
3. A supported Codex adapter with explicit task IDs, status and scoped messages;
   never assume a UI click macro or generic shell is that integration.
4. Spotify service-specific search/playback with its own account authorization.
5. Phone and ElevenLabs after the text/device lifecycle is stable.

Kairos, proactive delivery and nightly recalculation remain unchanged on the
legacy path. Before connecting them to device actions, their proposals must
enter this same permission lifecycle; background work cannot grant approval.
