# ADR 0005: PC control trial before memory, Kairos and NC

Status: implemented, Windows trial. This is a bounded set of 30 device tools,
not arbitrary computer control. Existing identity files, memory/context defaults,
Kairos and nightly entry points are unchanged. No background job was enabled.

## Added capabilities

| Tools | Policy / contract |
| --- | --- |
| list_media_sessions | Approval before app IDs/playback states go to the model; no track titles |
| control_media_session | Explicit play/pause/next/previous on a short-lived selected SMTC session; no global-key fallback |
| search_spotify | Approved web search; no automatic track selection or playback |
| get_audio_status / set_mute | Master audio readback, explicit mute/unmute, no toggle ambiguity |
| get_brightness / set_brightness | One supported internal WMI display; typed fixed method inputs and readback |
| get_disk_space | System drive capacity only, no volume/file inventory |
| open_settings | Approved fixed sound/display/Bluetooth/Wi-Fi/battery/notification page; does not change settings |
| lock_screen | Explicit confirmation; lock request only, not sleep/reboot/shutdown |
| list_folder / open_folder | Approved desktop/documents/downloads/workspace or validated relative subfolder |
| create_workspace_folder | Approved new directory in the separate EPIS workspace only; no overwrite |

The prior 17 device capabilities are retained. The CLI adds `/help`, `/workspace`
and `/cancel`, which do not call a model. The local test area is
`.epis-runtime/files`, lazily initialized on an approved workspace operation.

## Boundaries

Folder access rejects absolute paths, traversal, alternate streams, reserved
names, hidden relative components, and reparse ancestors/components. Listings
are nonrecursive and bounded to 100 entries / 500 inspected directory entries
and a 24 KB encoded-entry budget to fit the transport even with Unicode names;
hidden/system/reparse entries are omitted. File content reading/writing, copying,
moving, deletion and file execution are **not** exposed. New folder creation is
not allowed in the general Desktop/Documents/Downloads roots.

Path checks are application-level boundaries, not protection against a hostile
process concurrently rewriting directories as the same Windows user. Redirected
or OneDrive reparse folders may be refused. There is no arbitrary root setting.

Media tokens are replaced on each listing and expire after 120 seconds. App IDs
must match, and missing or ambiguous sessions are rejected. An already satisfied
play/pause request is a no-op. Accepted commands are distinct from verified state;
next/previous never claims a specific track transition. There is no Spotify OAuth
or library access. The old global media-key tools remain explicitly unverified.

Core, not the model, displays permission prompts. Function descriptions and the
agent boundary distinguish a tool proposal from approval. Native errors and
timeouts retain the existing unknown-outcome/no-automatic-replay behavior.
Permission, schema, short-lived selection and worker checks remain in code.

Profiles are not silently widened. Explicit enrollment for the trial:

```powershell
python -m pip install -r requirements.txt
python scripts/device_pairing.py init-local --profile .epis-runtime/pairing-pc --all-local-tools
python -X utf8 main.py --device-transport paired --pairing-dir .epis-runtime/pairing-pc --env-file D:\EPIS\Layer-3\keys.env
```

Do not rerun enrollment over an existing profile. PyWinRT projections are pinned
to 3.2.1 and were tested on Python 3.12 x64. Stdio is still the repository default;
the user's local launcher explicitly selects the separate paired profile.

## Verification and limits

- 100 unit/regression/integration tests and 12 parameterized subtests passed;
  compileall and git diff --check passed. No configured lint suite exists.
- Actual enrolled worker: cold media-session read, master mute read/write at the
  unchanged value, brightness read/write at the unchanged value, and disk read.
- The actual WMI test exposed a named-argument COM incompatibility and a driver
  returning no output object; fixed with typed method inputs and readback, with
  regression coverage. No UI automation or shell fallback was added.
- Actual app-launch adapter opened a purpose-built WinForms fixture through a
  synthetic catalog source. The paired worker minimized, maximized, restored,
  focused and normally closed only that owned fixture. This does not prove every
  installed application's launch/focus behavior. Actual installed discovery was
  verified in ADR 0004.
- Actual paired folder approval denial, creation, listing and no-overwrite checks
  passed in an owned temporary workspace directory, then removed that fixture.
- Actual SMTC command re-applied the current playback state and was accepted;
  no user's track was deliberately changed. Unsupported apps may expose no SMTC
  session, or reject a command. Playback transitions across all apps are unproven.
- Ten live Luna -> Core -> **synthetic transport** scenarios passed: app launch,
  window minimize, targeted pause, Spotify search, folder open/create, mute,
  brightness, settings and unsupported deletion. No real app/file inventory went
  to the model and no OS operation was possible in that harness. These are not
  ten full real-device E2E tests. Early runs exposed redundant conversational
  permission requests and a repetitive dummy-ID copying failure; descriptions
  were clarified and final fixtures use realistic opaque IDs with exact binding.
- The synthetic suite allows an extra approved discovery before window listing;
  that is inefficient but not an authorization bypass. Prompt behavior is not
  assumed deterministic. The final observed window case used the direct route.
- Locking the user's screen, changing real settings and opening external search
  pages were not performed during verification; their adapters have mocked or
  policy coverage. A page-opening request is not proof that the page loaded.
- Updated local launcher startup, help/workspace/device inspection and quit passed.

Run the opt-in synthetic API check (paid model requests):

```powershell
python scripts/smoke_pc_agent.py --env-file D:\EPIS\Layer-3\keys.env
```

The original live smoke remains a historical seven-case system/Luna/Sol check;
it is not proof of the new desktop surface. The new harness records no memories
and uses no real device worker. Secrets stay in the external environment file.

## Deferred intentionally

User trials come next. Memory/context integration follows only after the user's
continuation, then Kairos; NC (Nightly Calculation) follows longer stable use.
No identity file edits, historical memory conversion, NC scheduling, model switch,
cloud/phone/voice setup or Codex task integration is part of this increment.
Unrestricted shell, admin actions, screenshots/keyboard injection, shutdown and
destructive file operations are not claimed as supported.

## Primary references

- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling): explicit descriptions, tool-result loop; application retains execution authority.
- [Windows media sessions](https://learn.microsoft.com/en-us/uwp/api/windows.media.control.globalsystemmediatransportcontrolssession): targeted Windows media APIs.
- [Known folders](https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath): resolve configured user folder locations rather than guessing paths.
- [WMI brightness method](https://learn.microsoft.com/en-us/windows/win32/wmicoreprov/wmisetbrightness-method-in-class-wmimonitorbrightnessmethods): fixed Timeout/Brightness inputs.
- [PyWinRT](https://github.com/pywinrt/pywinrt): Python projections of Windows Runtime APIs.
