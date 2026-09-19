"""Opt-in live API smoke using the real CLI factory; blocks OS mutations."""
import argparse
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Layer-2" / "src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--device-transport", choices=["stdio", "paired", "inprocess"])
    parser.add_argument("--pairing-dir")
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(args.env_file)
    os.environ["EPIS_LUNA_CONTEXT_MODE"] = "minimal"
    if args.device_transport:
        os.environ["EPIS_DEVICE_TRANSPORT"] = args.device_transport
    if args.pairing_dir:
        os.environ["EPIS_PAIRING_DIR"] = args.pairing_dir
    from agentic.cli import create_core

    core = create_core()
    dispatched = []
    original = core.local_agent.execute
    from agentic.tasks import TaskStore
    core.tasks.close()
    core.tasks = TaskStore()

    def read_only(capability, arguments, **kwargs):
        dispatched.append(capability)
        if capability not in {"system.info", "system.battery"}:
            return {"ok": False, "error": "Live smoke blocks OS mutations"}
        return original(capability, arguments, **kwargs)

    core.local_agent.execute = read_only
    cases = [
        ("greeting", "Hey!"),
        ("system_info", "Şu an bilgisayarın durumu ne?"),
        ("devices", "EPIS'e kayıtlı hangi cihazlarım bağlı?"),
        ("battery", "Pil yüzde kaç ve şarj oluyor mu? get_battery ile kontrol et."),
        ("receipts", "EPIS'teki son cihaz işlemlerinin durumunu get_task_status ile kontrol et."),
        ("unsupported_song", "Spotify'dan Manifest-Snap şarkısını seçip çalır mısın?"),
        ("sol", "Sol'a delege et: kişisel asistanın offline cihaz komut kuyruğunda tekrar deneme ve çift işlem risklerini değerlendir; kısa bir çözüm planı çıkar."),
    ]
    # Same ContextBuilder and identity path as CLI. Test conversations are not
    # persisted to the user's memory; private context reads are tripwires.
    from contextlib import closing
    with closing(core), patch.object(core.memory, "log_interaction"), \
         patch.object(core.context_builder, "build", side_effect=AssertionError("private context read")):
        for label, prompt in cases:
            dispatched.clear()
            turn = core.handle(prompt)
            checks = bool(turn.message) and not turn.message.startswith('{"type"')
            if label == "greeting":
                checks &= not turn.tool_results
            elif label == "system_info":
                checks &= dispatched == ["system.info"] and all(r.get("ok") for r in turn.tool_results)
            elif label == "unsupported_song":
                checks &= not dispatched and not turn.tool_results
            elif label == "devices":
                checks &= any(r.get("ok") and r.get("devices") for r in turn.tool_results)
            elif label == "battery":
                checks &= dispatched == ["system.battery"] and all(r.get("ok") for r in turn.tool_results)
            elif label == "receipts":
                checks &= any(r.get("ok") and r.get("tasks") for r in turn.tool_results)
            elif label == "sol":
                checks &= len(turn.tool_results) == 1 and turn.tool_results[0].get("ok", False)
                checks &= turn.tool_results[0].get("model_used") == core.sol.model
            print(json.dumps({"case": label, "passed": bool(checks), "tools": dispatched,
                              "reply": turn.message}, ensure_ascii=False), flush=True)
            if not checks:
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
