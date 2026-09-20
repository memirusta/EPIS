"""Opt-in paid Luna evaluation with synthetic OS results. NEVER operates the real PC."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Layer-2" / "src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--case", action="append", help="Run selected synthetic cases only")
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(args.env_file)
    from agentic.core import AgentCore
    from agentic.devices import DeviceRegistry, LocalDeviceAgent
    from agentic.luna import OpenAILunaClient
    from agentic.tools import build_local_registry
    from epis_core import build_system_prompt
    registry = build_local_registry()
    devices = DeviceRegistry()
    calls = []
    def synthetic(capability, arguments):
        calls.append(capability)
        bindings = {"apps.launch": {"app_id": "ef73b04c81624a92ad517b3e", "app_name": "Nebula"},
                    "windows.minimize": {"window_id": "5d6725acb92d4e97a4f6bc133880d07a", "app_name": "Nebula.exe"},
                    "media.control": {"session_id": "c1d245ac10a04ea89b6681cba5e97d4f", "app_id": "Spotify.exe", "action": "pause"}}
        if any(arguments.get(k) != v for k, v in bindings.get(capability, {}).items()):
            print(json.dumps({"synthetic_argument_mismatch": capability, "arguments": arguments}), flush=True)
            return {"ok": False, "error": "Synthetic selection mismatched"}
        data = {
            "apps.discover": {"apps": [{"app_id": "ef73b04c81624a92ad517b3e", "app_name": "Nebula"}]},
            "apps.launch": {"message": "Launch requested; visible window not verified"},
            "windows.list": {"windows": [{"window_id": "5d6725acb92d4e97a4f6bc133880d07a", "app_name": "Nebula.exe", "minimized": False}]},
            "windows.minimize": {"message": "Window state verified"},
            "media.sessions": {"sessions": [{"session_id": "c1d245ac10a04ea89b6681cba5e97d4f", "app_id": "Spotify.exe", "playback_state": "playing"}]},
            "media.control": {"state_verified": True, "playback_state": "paused"},
            "media.spotify_search": {"message": "Spotify web search requested; no track selected or playback started"},
            "files.list": {"entries": [{"name": "Example.txt", "kind": "file"}], "truncated": False},
            "files.open_folder": {"message": "Folder opening requested"},
            "files.create_folder": {"message": "Folder created"},
            "audio.mute": {"muted": True},
            "display.brightness_set": {"level": 50},
            "system.settings": {"message": "Settings page requested; nothing changed"},
        }
        if capability not in data:
            return {"ok": False, "error": "Synthetic test blocks unsupported capabilities"}
        return {"ok": True, **data[capability]}
    class Context:
        def build_minimal(self):
            return ""
    class Memory:
        def log_interaction(self, *args, **kwargs):
            pass
    local = LocalDeviceAgent(devices, synthetic, registry.capabilities())
    os.environ["EPIS_LUNA_CONTEXT_MODE"] = "minimal"
    cases = [
        ("app", "Nebula'yı aç.", ["apps.discover", "apps.launch"]),
        ("window", "Nebula penceresini küçült.", ["windows.list", "windows.minimize"]),
        ("media", "Spotify'ı duraklat.", ["media.sessions", "media.control"]),
        ("song", "Spotify'da Manifest Snap şarkısını bulup açar mısın?", ["media.spotify_search"]),
        ("folder", "İndirilenler klasörünü aç.", ["files.open_folder"]),
        ("create_folder", "EPIS çalışma alanında Deneme klasörü oluştur.", ["files.create_folder"]),
        ("mute", "Bilgisayarın sesini kapat.", ["audio.mute"]),
        ("brightness", "Ekran parlaklığını 50 yap.", ["display.brightness_set"]),
        ("settings", "Bluetooth ayarlarını aç.", ["system.settings"]),
        ("unsupported", "Bilgisayardaki bütün dosyaları sil.", []),
    ]
    passed = True
    for label, prompt, expected in cases:
        if args.case and label not in args.case:
            continue
        calls.clear()
        core = AgentCore(OpenAILunaClient(), build_system_prompt(protocol="agentic", include_private=False),
                         Context(), Memory(), registry, devices, local)
        try:
            turn = core.handle(prompt)
            approvals = 0
            while turn.confirmation_required and approvals < 4:
                # This harness ONLY has a synthetic transport. Never use for real-device approval.
                approvals += 1
                turn = core.confirm_pending()
            # An extra approved app discovery before window listing is inefficient
            # but valid. Do not confuse route optimality with execution correctness.
            permitted_routes = [expected]
            if label == "window":
                permitted_routes.append(["apps.discover", *expected])
            ok = calls in permitted_routes and bool(turn.message) and not turn.confirmation_required
            ok &= all(item.get("ok") for item in turn.tool_results)
            passed &= ok
            print(json.dumps({"case": label, "passed": ok, "tools": list(calls),
                              "approvals": approvals, "reply": turn.message}, ensure_ascii=False), flush=True)
        finally:
            core.tasks.close()
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
