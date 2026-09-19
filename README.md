# EPIS

**Emotional Personal Intelligence System**  
*A model-agnostic architecture for persistent personal AI.*

EPIS is an experimental long-term personal AI architecture built around one principle: **models should be replaceable; identity should not be.**

Instead of storing the whole concept of a personal AI inside one model checkpoint, EPIS separates conversational inference from memory, values, identity, routing, privacy, proactive behavior, and long-term learning.

> **Status:** working experimental implementation. EPIS 0.1 adds a text-first, capability-based agent loop while preserving its existing memory, privacy, Kairos, and nightly systems.

## Why EPIS?

Foundation models change quickly. A long-term personal system should not have to lose years of memory, behavior, and context every time its base model changes. EPIS therefore keeps the persistent parts of the system outside the model and treats inference providers as replaceable components.

## Architecture

```mermaid
flowchart LR
    U[Text now / Voice later] --> L[Luna: frontline EPIS voice]
    L --> C[EPIS Core: deterministic orchestrator]
    C --> M[Memory + Context + Privacy]
    C --> T[PermissionEngine + ToolRegistry]
    T --> D[Capability-based Device Agent]
    L -. complex analysis only .-> S[Sol]
    S --> L
    K[Kairos / Nightly Recalculation] --> M
```

- **Luna — Frontline:** the normal user-facing EPIS voice, context consumer, and tool suggester.
- **EPIS Core:** deterministic memory/context assembly, permissions, tool dispatch, device routing, task state, and policy boundary.
- **Sol — Specialist:** a privacy-aware heavy-analysis delegate for code, planning, repository work, and long reasoning; it is not used for ordinary tools.

See [`docs/architecture.md`](docs/architecture.md) for details.

## Implemented areas

- model routing and external API clients
- persistent file-backed context and memory management
- privacy-aware name/place tokenization before external calls
- proactive delivery and Kairos scheduling
- nightly recalculation pipeline
- local model / API backend switching
- web chat interface
- optional WhatsApp bridge
- optional PC/phone/Gadgetbridge sensor integrations
- encryption helpers for private local data
- fine-tuning dataset architecture

## Current limitation

The surrounding architecture is functional, but small models that fit comfortably on consumer GPUs have not consistently met the target for natural conversation, multilingual quality, long-context adherence, personality consistency, and reasoning.

EPIS is therefore being used to evaluate a hybrid approach: local/open-weight frontline models plus selectively routed external reasoning and periodic fine-tuning.

## EPIS 0.1 quick start

> This repository ships with **no personal data and no credentials**. Monitoring integrations are disabled by default.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -r requirements.txt
python scripts/bootstrap_public_data.py
```

Create the runtime environment file:

```bash
# Windows
copy Layer-3\keys.env.example Layer-3\keys.env

# Linux/macOS
cp Layer-3/keys.env.example Layer-3/keys.env
```

Fill only the providers/integrations you intend to use, then start the UI using the scripts in `scripts/` or the project launcher.

For the 0.1 text agent, configure the direct OpenAI Luna/Sol models in the ignored `Layer-3/keys.env` file (or process environment):

```text
OPENAI_API_KEY=...
LUNA_MODEL=gpt-5.6-luna
LUNA_REASONING_EFFORT=none
SOL_MODEL=gpt-5.6-sol
SOL_REASONING_EFFORT=high
EPIS_LUNA_CONTEXT_MODE=minimal
```

Then run:

```bash
python main.py
```

An existing environment file can be used without copying it into this checkout:

```powershell
python -X utf8 main.py --env-file D:\EPIS\Layer-3\keys.env
```

The Windows launcher also accepts a specific Python installation:

```powershell
.\start-agent.ps1 -Python .\.venv\Scripts\python.exe -EnvFile D:\EPIS\Layer-3\keys.env
```

The CLI now starts an owned, hidden Device Worker process using private pipes;
Core no longer runs the Windows handlers in its own process by default. No
network port or startup service is installed. The worker exits with the CLI.
`EPIS_DEVICE_TRANSPORT=inprocess` is an explicit compatibility option.

An optional enrolled local TLS mode is also implemented. Explicit one-time
setup creates encrypted Windows user-bound keys and a capability grant:

```powershell
python scripts/device_pairing.py init-local --all-local-tools
python -X utf8 main.py --env-file D:\EPIS\Layer-3\keys.env --device-transport paired
```

Omit `--all-local-tools` at setup for read-only system/battery scope. Profiles
are never overwritten silently. The paired mode is **loopback-only**, not
phone/cloud access: it verifies mutual certificates, enforces revocable
capability scope, sends heartbeats and retains encrypted device receipts
across restarts. It does not change the firewall, system trust store or Windows
startup settings. See [ADR 0003](docs/adr/0003-paired-local-tls.md) for setup,
revocation, validation, DPAPI limitations and the remaining remote work.

Use `/devices` to inspect availability/capabilities, `/tools` for the permitted
tool list, and `/tasks` for recent action receipts, without calling a model.
Luna also has `get_devices` and `get_task_status` for conversational requests.
`get_battery`, `media_next`, `media_previous` and approved HTTPS `open_url` are
now available in addition to the first five tools. App closing and URL opening
require approval, valid for 120 seconds. Use only one CLI per checkout.

Interrupted actions are recorded as unknown and are not replayed automatically.
The local journal contains metadata only, not command arguments or conversation
bodies. See [ADR 0002](docs/adr/0002-local-device-process.md) for the process,
permission and lifecycle boundaries, verification and remaining remote work.

These launch a Python file directly and work in Windows PowerShell 5.1; no
inline `python -c` quoting or Base64 is needed. Normal CLI output is conversation
only; `--debug` shows diagnostic events. Event logs are written to ignored
`epis.log` without conversation bodies or raw API exception messages.

Try `Bilgisayarın durumu ne?`, `Spotify'ı aç`, `Sesi 20 yap`, or `Medya oynat/duraklat tuşunu gönder`.
The media tool is a global toggle: it cannot guarantee play versus pause,
target Spotify, or search/select a specific track. EPIS explains this limitation
for a song request without sending an unrelated playback signal.
The observed tool result is returned to Luna before EPIS answers. The legacy
terminal flow remains available as `python main.py --legacy`; existing
web/WhatsApp and Kairos paths stay on their compatibility path during migration.
Luna delegates at most one complex analysis/coding/planning/repository task per
turn to Sol, then synthesizes Sol's result back into EPIS's voice. Ordinary
conversation and local device actions stay on Luna.

`minimal` skips private context collection entirely, including Gadgetbridge,
screen and phone reads. Shared EPIS personality/values are retained, but the
private user seed/profile are omitted. Messages you type, session history,
and requested tool results still go to the model API. New interactions are
stored by the existing local MemoryManager. `local` is a legacy opt-in to full
context; it does **not** move inference locally. With the default OpenAI client,
that mode sends collected private context to OpenAI. Unknown mode names fail.

Validation (live smoke uses paid API calls, blocks OS mutations, and sends
synthetic prompts plus actual system/battery, device and task metadata to the
configured model APIs; run only if you authorize that transmission):

```powershell
python -m unittest discover -s tests
python scripts/smoke_agent.py --env-file D:\EPIS\Layer-3\keys.env
```

The smoke accepts `--device-transport paired`. In paired mode, encrypted device
tool receipts persist for deduplication; test conversation-memory writes and
Core action-journal persistence are disabled. The 68-test suite includes real
loopback TLS and Windows DPAPI checks; run it as the normal Windows user, not
an account without a loaded DPAPI profile. No external API calls occur in the
unit/integration suite.

See [`docs/adr/0001-agentic-pivot.md`](docs/adr/0001-agentic-pivot.md) for the migration map, permissions, cloud/device boundary, and follow-up milestones.

The optional WhatsApp bridge has its own Node.js dependencies:

```bash
cd whatsapp_bridge
npm install
```

## Repository safety

The original working project contains extremely private personal-AI data. This public repository deliberately excludes it. The `.gitignore` is designed so that local runtime identity, memory, logs, keys, browser sessions, and sensor databases do not get committed later.

See [`docs/public-sanitization.md`](docs/public-sanitization.md) and [`docs/privacy.md`](docs/privacy.md).

## Fine-tuning

`training/final_dataset.example.jsonl` contains **synthetic examples only**. Real conversational datasets should remain private and curated. The long-term design treats the dataset as portable across base-model upgrades rather than treating any LoRA adapter as the identity itself.

See [`docs/fine-tuning.md`](docs/fine-tuning.md).

## Project structure

```text
EPIS/
├─ Layer-1/             # public identity templates; private runtime data is ignored
├─ Layer-2/src/         # Python orchestration, memory, privacy, sensors, UI
├─ Layer-3/             # environment template only; no keys
├─ docs/                # architecture, privacy and fine-tuning notes
├─ examples/            # synthetic runtime templates
├─ scripts/             # launch/setup/test helpers
├─ training/            # synthetic fine-tuning example
├─ whatsapp_bridge/     # optional WhatsApp Web bridge
├─ main.py
└─ requirements.txt
```

## Security note

This is an experimental personal project, not a production security-reviewed platform. Review network exposure, authentication, third-party API data handling, and local encryption before using it with real personal data.

## License

No open-source license has been selected yet. The source is published for technical evaluation and demonstration.
