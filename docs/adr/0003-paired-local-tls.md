# ADR 0003 — Enrolled local mTLS and restart-safe device receipts

Status: implemented and tested on Windows, 2026-09-20. This is a local pairing
increment, **not a deployed cloud service or phone connection**.

## Decision

Retain stdio as the default, add explicit `paired` transport, and keep the
model/memory/legacy orchestration interfaces unchanged. The paired launcher
creates an ephemeral **127.0.0.1-only** listener and starts an owned outgoing
device client process. Both endpoints require TLS certificates. After the
single connection is accepted the listening socket closes. No public binding,
firewall rule, system trust-store change or Windows startup service is added.

The local CA and enrollment policy are application files, not OS trust anchors.
Mutual TLS authenticates the connection; Core remains the authorization
boundary for user actions, with additional checks at the device.

## Offline pairing and privilege scope

`pairing.py` supports generating a device key/CSR locally, approving its signed
P-256 CSR using the authority, and installing the matching client certificate.
CSR signatures, device identifiers and public-key type are validated; an
installed certificate must match its private key, device ID and supplied CA.
Existing key/profile directories are never silently overwritten.

The local convenience command creates authority and device identities on this
same Windows account. It is a harness for the enrollment and transport layers,
not a claim that a second machine was enrolled. Future cross-machine enrollment
must generate the device key on that machine and move only the CSR and signed
public certificates through a trusted channel.

Client certificates contain an issuer-signed capability scope in SAN URIs.
Core also stores the approved fingerprint, device ID, scope, expiry and revoke
flag in `grants.db`. These grants are not model-editable. Certificate scope,
current grant scope and actual worker capabilities must all permit an action.
Sensitive tools still require the Core's per-action, expiring user approval;
enrollment never bypasses `close_app`/`open_url` confirmation.

The same-user CA key owner is trusted to enroll devices. This is not a sandbox
against other malicious code running as that Windows user. The profile is
excluded from Git using `.epis-runtime/`.

## Credential and response storage

P-256 private keys are stored as encrypted PKCS8. Their random passphrases are
protected with Windows user-scoped DPAPI, with UI disabled; machine-wide
decryption scope is not used. No plaintext passphrase or private-key temporary
file is required by TLS setup. DPAPI failure blocks pairing; it does not fall
back to plaintext. A real logged-in Windows profile is required; restricted
test accounts without DPAPI credentials may not run these tests.

Paired device responses are encrypted with the same user-scoped DPAPI before
being persisted to `device/receipts.db`. Only request IDs and canonical payload
SHA-256 hashes are plaintext. Core's separate action journal remains metadata
only. The worker does not import model clients, memory collectors or dotenv.
It receives the same allowlisted OS environment as the stdio worker, not API
keys from the parent's environment.

DPAPI limits portability: copying this profile to another Windows user or a
Linux cloud host is not a migration method. Cloud key storage and certificate
renewal/rotation still require an explicit implementation and deployment plan.
Certificates expire (leaf 90 days; local CA 365 days); no auto-renewal exists.

## Lifecycle and failure semantics

- Every request checks the current grant; revocation/expiry fails closed.
- Idle heartbeat every five seconds verifies the authenticated connection;
  a missed response or revoked grant closes it. It does not reconnect/replay.
- Requests/responses are bounded to 64 KiB and matched by request ID; the
  paired request timeout is five seconds. Long model calls do not stop the
  heartbeat thread.
- Worker checks target, signed capability scope, schema, deadline and approval
  before executing **or returning cached results**.
- An atomic SQLite reservation commits an encrypted unknown receipt before
  the OS action. After completion, its observed response replaces that receipt.
  Duplicate IDs return the prior response across worker restarts. Different
  arguments under the same ID are rejected.
- A crash after reservation but before a durable result leaves unknown, never
  permission to repeat. Core stops further tool execution in that turn.
- Receipts are not automatically evicted: new actions fail closed at 4096
  entries. A safe retention/rotation protocol is future work; simply deleting
  the receipt DB would discard replay protection and is not recommended.
- A corrupted/unavailable grant DB fails closed; no permission fallback.
- Exit closes the authenticated connection and owned helper process only.

These are conservative at-most-once dispatch mechanisms, **not a distributed
exactly-once OS-effect guarantee**. No offline command queue is implemented.

## Usage

Use the same Python environment as EPIS. One-time setup defaults to read-only
system/battery capabilities:

```powershell
python scripts/device_pairing.py init-local
```

To deliberately enroll all nine existing constrained Windows capabilities:

```powershell
python scripts/device_pairing.py init-local --all-local-tools
```

Do not run both setup commands for the same profile. Existing identities are
preserved, and the second setup attempt fails. Then start paired mode:

```powershell
python -X utf8 main.py --env-file D:\EPIS\Layer-3\keys.env --device-transport paired
```

`start-agent.ps1` accepts `-Transport paired` and optional `-PairingDir`.
Default profile: `.epis-runtime/pairing` in the checkout. `/devices`, `/tools`,
`/tasks` and all existing text interactions remain available. Stdio fallback
is explicit (`--device-transport stdio`), never automatic after pairing failure.

To revoke a device, explicitly select its registered ID:

```powershell
python scripts/device_pairing.py revoke --device-id YOUR_DEVICE_ID
```

This affects future requests and heartbeat checks; it cannot undo an OS action
already executed or in flight. Automatic re-enrollment is intentionally absent.

## Verification

68 unit/integration tests passed in the normal Windows user context, including:
mutual TLS, missing/wrong certificates, wrong hostname, signed-but-unenrolled
client, scope restrictions, live revocation, expiry, DB failure, cached results
after narrower enrollment, lost replies, durable reservations/restart replay,
encrypted credentials/responses, heartbeat, CLI factory and process cleanup.
PowerShell 5.1 paired startup/inspection/quit passed as well.

All seven authorized live Luna/Sol smoke cases passed over paired mode:
greeting, system information, devices, battery, task receipts, unsupported
Spotify song without unrelated playback, and Luna -> Sol -> Luna synthesis.
No OS mutations were performed by that smoke; full context retrieval and
conversation-memory writes were blocked. Its Core task journal is transient,
but the paired device's encrypted tool receipts intentionally persist for
deduplication. Do not confuse this with cloud memory synchronization.

No repository linter was configured; compilation and whitespace checks are
run in addition to tests. This implementation has not had an independent
production security review and is intentionally not exposed off-machine.

## Remaining work before real remote use

Standalone outgoing agent lifecycle, server deployment and hostname certificates,
trusted cross-machine enrollment UX, renewal/rotation, remote revocation and
device policy distribution, bounded receipt retention, service health and
operational recovery. Phone client, Spotify account integration, actual Codex
adapter, ElevenLabs and Tauri are separate milestones, not completed here.

## Primary references

- [Python TLS verification and client authentication](https://docs.python.org/3/library/ssl.html)
- [cryptography certificate/CSR construction](https://cryptography.io/en/stable/x509/tutorial/)
- [Windows user-scoped DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)
