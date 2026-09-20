"""Explicit offline local pairing management. Never installs a service or exposes a port."""
import argparse
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Layer-2" / "src"))
from agentic.pairing import GrantStore, bootstrap_local_pair
from agentic.tools import build_local_registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["init-local", "revoke"])
    parser.add_argument("--profile", type=Path, default=ROOT / ".epis-runtime" / "pairing")
    parser.add_argument("--device-id", default=socket.gethostname().lower())
    parser.add_argument("--capability", action="append", dest="capabilities")
    parser.add_argument("--all-local-tools", action="store_true", help="Explicitly grant the currently registered Windows tools, still subject to Core confirmation")
    args = parser.parse_args()
    if args.operation == "init-local":
        if args.all_local_tools and args.capabilities:
            parser.error("Choose capabilities OR all-local-tools")
        capabilities = (build_local_registry().capabilities("windows") if args.all_local_tools
                        else set(args.capabilities or ["system.info", "system.battery"]))
        bootstrap_local_pair(args.profile, args.device_id, capabilities)
        print("Local pairing profile created; encrypted credentials stay on this Windows account.")
        print("Granted capabilities: " + ", ".join(sorted(capabilities)))
        print("No public endpoint or startup service was installed.")
    else:
        if not (args.profile / "authority" / "grants.db").is_file():
            parser.error("Pairing profile not found")
        if not GrantStore(args.profile / "authority" / "grants.db").revoke(args.device_id):
            parser.error("Device not found")
        print("Device grant revoked; active local TLS connections will close on policy check/heartbeat.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Pairing failed: {type(exc).__name__}. Existing identities were not overwritten.", file=sys.stderr)
        raise SystemExit(1)
