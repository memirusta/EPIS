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
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(args.env_file)
    os.environ["EPIS_LUNA_CONTEXT_MODE"] = "minimal"
    from agentic.cli import create_core

    core = create_core()
    dispatched = []
    original = core.local_agent.dispatcher

    def read_only(capability, arguments):
        dispatched.append(capability)
        if capability != "system.info":
            return {"ok": False, "error": "Live smoke blocks OS mutations"}
        return original(capability, arguments)

    core.local_agent.dispatcher = read_only
    cases = [
        ("greeting", "Hey!"),
        ("system_info", "Şu an bilgisayarın durumu ne?"),
        ("unsupported_song", "Spotify'dan Manifest-Snap şarkısını seçip çalır mısın?"),
        ("sol", "Sol'a delege et: kişisel asistanın offline cihaz komut kuyruğunda tekrar deneme ve çift işlem risklerini değerlendir; kısa bir çözüm planı çıkar."),
    ]
    # Same ContextBuilder and identity path as CLI. Test conversations are not
    # persisted to the user's memory; private context reads are tripwires.
    with patch.object(core.memory, "log_interaction"), \
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
