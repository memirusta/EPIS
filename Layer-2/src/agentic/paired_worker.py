"""Outbound-only local TLS device client. No LLM, private memory or public listener."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography import x509
from agentic.device_worker import DeviceWorker
from agentic.pairing import DurableReceipts, certificate_capabilities, client_context
from agentic.paired_transport import FramedTLS


def run(profile, port):
    profile = Path(profile)
    identity = json.loads((profile / "identity.json").read_text(encoding="utf-8"))
    os.environ["EPIS_DEVICE_ID"] = identity["device_id"]
    certificate = x509.load_pem_x509_certificate((profile / "device.crt").read_bytes())
    worker = DeviceWorker(receipt_store=DurableReceipts(profile / "receipts.db"),
                          allowed_capabilities=certificate_capabilities(certificate))
    context = client_context(profile)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname="localhost") as connection:
            peer = x509.load_der_x509_certificate(connection.getpeercert(binary_form=True))
            expires = min(peer.not_valid_after_utc.timestamp(), certificate.not_valid_after_utc.timestamp())
            connection.settimeout(20)
            frame = FramedTLS(connection)
            try:
                while time.time() < expires:
                    request = frame.receive()
                    if time.time() >= expires:
                        raise PermissionError("Peer or device certificate expired")
                    result = worker.handle(request)
                    frame.send({"version": 1, "id": request.get("id"), "result": result})
            finally:
                frame.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    try:
        run(args.profile, args.port)
    except (OSError, ValueError, KeyError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
