# EPIS Capability Broker architecture

## Goal

Luna decides **what the user wants**. Core and Capability Broker decide **how the
goal is executed**. Luna should not learn OS recipes, provider URLs, executable
names, MCP endpoints, screenshot loops, retry policy, or provider-specific APIs.

The stable flow is:

```text
User
  -> Luna (intent + semantic capability)
  -> AgentCore (policy + task receipt)
  -> CapabilityBroker
       -> deterministic/local adapter
       -> hosted provider
       -> interactive Computer Use provider
       -> connector/MCP provider
       -> media/voice/image provider
       -> automation provider
  -> verified result
  -> Luna (natural response)
```

## Rules

1. **Semantic tools are stable.** A tool is named after the user intent
   (`web_research`, `computer_execute_goal`, `vision_analyze`), not an
   implementation detail such as Ctrl+L or a specific vendor endpoint.
2. **Provider choice is Core-owned.** Luna never chooses raw provider IDs, server
   URLs, executable paths, vector-store IDs, secrets, or device internals.
3. **Only live capabilities are exposed.** A semantic tool is visible to Luna only
   when at least one trusted provider reports it as available.
4. **Fallback is fail-closed.** Broker may fall through only when a provider
   explicitly says it is unavailable/unsupported. It does not retry a normal
   failure or an unknown outcome on a second provider.
5. **Permissions remain deterministic.** CapabilitySpec carries the same risk and
   confirmation metadata used by PermissionEngine. A provider cannot grant itself
   permission.
6. **No recipe prompt growth.** New providers should implement contracts instead
   of adding "first do X, then Y" instructions to Luna's prompt.
7. **Runtime state is separate from memory.** WorldState describes verified,
   short-lived facts; personal memory remains a different subsystem.

## Provider families

### Local deterministic adapters

Existing Windows, browser, Spotify, files, shell, media and UIA adapters remain
the preferred path when a dedicated implementation exists.

### Hosted intelligence

`OpenAIHostedProvider` initially supplies:

- `web.research` using hosted web search
- `code.analyze` using hosted Code Interpreter
- `vision.analyze` for images already available as HTTPS/data URLs
- `files.hosted_search` when trusted vector-store IDs are configured by the host

These operations do not mutate the user's PC.

### Computer Use

Computer Use is a separate interactive provider because the model-side loop and
the target desktop are on different sides of the device boundary in cloud mode.

`OpenAIComputerUseProvider` implements this path. It keeps the Responses loop
stateless (`store=False`) and requests encrypted reasoning content so response
items can be replayed. The Windows device exposes only two hidden primitives:
frame capture and frame-bound action execution. Luna sees only
`computer_execute_goal`.

The provider/device path owns:

- deterministic app discovery/launch/focus when `target_app` is supplied
- screenshot capture from the selected target window/device
- structured click/type/keypress/scroll/drag/move execution
- opaque frame IDs so coordinates cannot be replayed against another screenshot
- target-window identity and foreground-process guards
- password-field and elevation/UAC guards
- one goal-level EPIS approval before screenshots/input begin
- unexpected-app-switch guard
- no-progress loop detection
- screenshot feedback until goal completion

There is deliberately **no fixed total action-count or wall-clock limit** in the
architecture. Bounded action batches and screenshot frame sizes protect the
transport; workflow stopping is state-based.

### Authorization and verified side effects

Computer Use authorization is intent-aware but remains Core-owned.

- `explicit_current_turn`: when the user's current message directly asks for the
  semantic action, EPIS does not ask the same question a second time.
- `session_category_grant`: confirming a grantable category lets related
  user-directed actions continue for the current conversation. `/new` clears
  these grants.
- `assistant_proposed`: if EPIS expands the request with a new consequential
  action, it pauses and asks what it plans to do before executing it.
- explicit user denial overrides any session grant.
- credential entry, payments/purchases, privileged security/elevation and
  destructive changes remain separately confirmation-bound and are not granted
  for the session.

A session grant never turns an unrelated user message into permission for a new
side effect. The current message still has to be relevant to that category.

Computer Use completion is verification-bound. The visual sub-agent must inspect
a post-action screenshot and return `VERIFIED:` only when the latest screen
visibly proves the goal. `UNVERIFIED:` or a missing verification marker is
returned to Core as an unverified failure rather than a success.

### Connectors / MCP

Trusted connector configuration belongs to the host, never Luna. The semantic
tool should name a configured connector alias and task; Core maps that alias to
trusted MCP configuration and bridges any provider approval requests into EPIS's
existing approval UI.

### Image, voice and realtime

Image generation, transcription, TTS and realtime voice are provider families,
not Luna prompt recipes. Binary artifacts/audio streams need an artifact/stream
transport contract before these capabilities are advertised live.

### Automations

Scheduled and condition-based tasks should use a scheduler provider. The scheduler
stores the user's declared goal and cadence/condition, then invokes the same
Capability Broker when the task runs.

## Self-improvement boundary

Self-improvement is a workflow built on capabilities, not permission to silently
rewrite production:

```text
observe failure/log
  -> inspect current repository
  -> Sol analysis
  -> create isolated branch/worktree
  -> generate patch
  -> tests/lint/regression
  -> show diff + test evidence to user
  -> explicit approval
  -> merge/deploy
```

EPIS may diagnose, propose, patch and test in isolation. Production mutation,
merge and deployment remain explicit user-controlled boundaries.

## Mobile client

The future phone app is a thin authenticated client:

```text
EPIS Mobile
  <-> HTTPS/WebSocket
Heroku AgentCore + CapabilityBroker
  <-> device routing
Windows/other device agents
```

Chat, voice, devices, task progress and approvals can therefore share one backend
without moving desktop-specific implementation details into the mobile app.
