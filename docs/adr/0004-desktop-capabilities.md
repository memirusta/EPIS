# ADR 0004: Bounded desktop discovery and window control

Status: implemented on Windows; not a universal computer-control or remote-access layer.

## Scope

Extend the existing registry and worker rather than introducing a second agent loop.
The registry now contains 17 device tools. Core-only device/task inspection and
Sol delegation are not counted as device tools.

| Tools | Capability | Confirmation |
| --- | --- | --- |
| discover_apps | apps.discover | Yes, before app names go to the model |
| launch_discovered_app | apps.launch | Yes, for the selected ID and name |
| list_windows | windows.list | Yes, before process names go to the model |
| focus/minimize/maximize/restore_window | windows.* | No, after a valid selection |
| close_window | windows.close | Yes; normal WM_CLOSE, never process termination |

The original nine tools remain available. The same definitions and validation
run in stdio, paired and explicit in-process modes. Inventory disclosure notices
are trusted registry metadata rendered by Core, not text composed by a model.

## Application discovery

Read App Paths registrations and at most 512 Start Menu shortcuts. Do not follow
reparse directories/shortcuts, launch a shortcut, pass its arguments, or accept
a model-supplied executable path. Keep local existing EXE targets only; reject
UNC paths, alternate streams, scripts and known command/interpreter hosts.
Return at most 50 query-matching names and opaque IDs, without filesystem paths.

Launch re-resolves the catalog and matches both ID and name. IDs include path,
size and modification time; they are not code signatures or protection against
a hostile process running as the same Windows user. Installed software is not
automatically trustworthy. User confirmation is still required. This is not an
exhaustive Store/portable-app index or a general command runner.

## Window selection and results

Read visible unowned top-level window process names and minimized state, never
window titles or document content. Keep at most 64 random token mappings inside
the worker. Tokens expire after 120 seconds and a new listing replaces them.
Before action, recheck HWND, PID, process creation time/name and window class.
If multiple current windows belong to the same application, refuse the action:
the available metadata cannot tell which document the user meant.

Minimize/maximize/restore/focus report observed Windows state. Foreground focus
restrictions are not bypassed. Close reports a request, not guaranteed closure:
an application's save dialog can remain. Launch similarly reports a request,
not proof that an application finished loading. Native identity checking has a
small check/use race and does not establish an OS-level security sandbox.

## Enrollment and privacy

Existing paired certificates retain their existing capability grants. Updating
code does not widen them. Explicitly enroll a separate profile if desktop scope
is wanted; do not overwrite an existing profile:

```powershell
python scripts/device_pairing.py init-local --profile .epis-runtime/pairing-desktop --all-local-tools
python main.py --device-transport paired --pairing-dir .epis-runtime/pairing-desktop --env-file D:\EPIS\Layer-3\keys.env
```

Normal approved tool results are sent to the configured model and can remain
in session history. Paired receipts retain the results encrypted for replay
protection. Core task records contain metadata only. Inventory is not collected
before its confirmation. No continuous monitoring, screenshot capture, public
listener, startup service, filesystem deletion, admin command or keyboard/mouse
automation was added.

## Verification

- 79 unit/regression/integration tests passed, including real local TLS/DPAPI.
- Synthetic model -> discovery approval -> result -> separate launch approval ->
  final reply verified without real app launch or external model calls.
- Negative cases: stale/mismatched IDs, changed executable metadata, rejected
  paths/extra command arguments, expired/replaced/reused window identities,
  ambiguous same-app windows, denied inventory and failed foreground focus.
- Actual Windows catalog discovery and approved/denied inventory round trips
  through an enrolled paired worker passed locally; no inventory was uploaded
  to a model during testing.
- Minimize/maximize/restore were verified on an owned temporary native window,
  then the fixture was removed. User windows were not changed.
- Arbitrary discovered-app launch, close and focus have mocked coverage, not
  comprehensive real-app E2E evidence. Existing seven live Luna/Sol smoke cases
  predate this increment; they do not verify the new desktop tools.
- compileall and git diff --check passed. No configured lint suite exists.

## References

- [Windows application registration](https://learn.microsoft.com/en-us/windows/win32/shell/app-registration)
- [SetForegroundWindow restrictions](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow)
- [Windows window features](https://learn.microsoft.com/en-us/windows/win32/winmsg/window-features)
