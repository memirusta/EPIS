import os
from pathlib import Path
import socket
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))
from agentic.device_worker import DeviceWorker
from agentic.devices import DeviceRegistry
from agentic.pairing import (GrantStore, DurableReceipts, bootstrap_local_pair, create_authority,
    create_device_request, approve_request, install_device_certificate, client_context,
    server_context, certificate_capabilities, unprotect, database)
from agentic.paired_transport import PairedLocalAgent
from agentic.tools import ToolRegistry, ToolSpec
from cryptography import x509
from cryptography.hazmat.primitives import serialization


@unittest.skipUnless(os.name == "nt", "Windows DPAPI and paired local integration")
class PairingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.profile = Path(cls.folder.name) / "paired"
        bootstrap_local_pair(cls.profile, "test-legion", {"system.info", "system.battery"})

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_real_mutual_tls_and_readonly_worker(self):
        agent = PairedLocalAgent(DeviceRegistry(), self.profile, heartbeat_interval=0.1)
        try:
            self.assertEqual(agent.device.device_id, "test-legion")
            self.assertEqual(agent.device.capabilities, {"system.info", "system.battery"})
            self.assertTrue(agent.transport.connection.cipher())
            self.assertTrue(agent.execute("system.info", {}, request_id="status-once")["ok"])
            self.assertTrue(agent.execute("system.battery", {}, request_id="battery-once")["ok"])
            self.assertFalse(agent.execute("app.open", {"app": "notepad"})["ok"])
            initial = agent.device.last_seen
            deadline = time.monotonic() + 3
            while agent.device.last_seen == initial and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertNotEqual(initial, agent.device.last_seen)
        finally:
            agent.close()
        self.assertIsNotNone(agent.process.poll())

    def test_persisted_receipt_replayed_without_handler_after_restart(self):
        first = PairedLocalAgent(DeviceRegistry(), self.profile)
        try:
            result = first.execute("system.info", {}, request_id="durable-live-replay")
            self.assertTrue(result["ok"])
        finally:
            first.close()
        second = PairedLocalAgent(DeviceRegistry(), self.profile)
        try:
            self.assertEqual(second.execute("system.info", {}, request_id="durable-live-replay"), result)
            conflict = second.execute("system.battery", {}, request_id="durable-live-replay")
            self.assertFalse(conflict["ok"])
            self.assertIn("different payload", conflict["error"])
        finally:
            second.close()

    def test_revocation_closes_live_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / "revoked"
            bootstrap_local_pair(profile, "revocable", {"system.info"})
            agent = PairedLocalAgent(DeviceRegistry(), profile, heartbeat_interval=0.05)
            try:
                self.assertTrue(GrantStore(profile / "authority" / "grants.db").revoke("revocable"))
                deadline = time.monotonic() + 3
                while agent.device.online and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(agent.device.online)
                self.assertFalse(agent.execute("system.info", {})["ok"])
            finally:
                agent.close()
            with self.assertRaises(PermissionError):
                PairedLocalAgent(DeviceRegistry(), profile)

    def test_keys_are_encrypted_and_password_is_dpapi_protected(self):
        key = (self.profile / "device" / "device.key").read_bytes()
        self.assertIn(b"BEGIN ENCRYPTED PRIVATE KEY", key)
        with self.assertRaises(TypeError):
            serialization.load_pem_private_key(key, password=None)
        protected = (self.profile / "device" / "device.password.dpapi").read_bytes()
        secret = unprotect(protected)
        self.assertNotEqual(secret, protected)
        self.assertEqual(len(secret), 32)

    def test_no_credentials_overwrite(self):
        original = (self.profile / "device" / "device.key").read_bytes()
        with self.assertRaises(ValueError):
            bootstrap_local_pair(self.profile, "other", {"system.info"})
        with self.assertRaises(ValueError):
            create_device_request(self.profile / "device", "other")
        self.assertEqual((self.profile / "device" / "device.key").read_bytes(), original)

    def test_unknown_capability_not_enrolled(self):
        with self.assertRaises(ValueError):
            approve_request(self.profile / "authority", self.profile / "device" / "device.csr", {"shell"})

    def test_certificate_scope_is_signed_and_readable(self):
        cert = x509.load_pem_x509_certificate((self.profile / "device" / "device.crt").read_bytes())
        self.assertEqual(certificate_capabilities(cert), {"system.info", "system.battery"})

    def test_wrong_certificate_cannot_replace_local_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            create_device_request(folder, "other")
            with self.assertRaises(ValueError):
                install_device_certificate(folder, (self.profile / "device" / "device.crt").read_bytes(),
                                           (self.profile / "authority" / "ca.crt").read_bytes())

    def test_grant_expiry_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            grants = GrantStore(Path(folder) / "grants.db")
            certificate = x509.load_pem_x509_certificate((self.profile / "device" / "device.crt").read_bytes())
            fingerprint = grants.add(certificate, "test-legion", {"system.info"})
            self.assertIsNotNone(grants.get(fingerprint))
            with database(grants.path) as db:
                db.execute("UPDATE grants SET expires=0")
            self.assertIsNone(grants.get(fingerprint))

    def _handshake(self, client, server_hostname="localhost"):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        failures = []
        def accept():
            try:
                raw, _ = listener.accept()
                raw.settimeout(3)
                with raw, server_context(self.profile / "authority").wrap_socket(raw, server_side=True) as secured:
                    secured.sendall(b"ready")
            except (OSError, ValueError) as exc:
                failures.append(type(exc).__name__)
        thread = threading.Thread(target=accept)
        thread.start()
        try:
            with socket.create_connection(listener.getsockname(), timeout=3) as raw:
                with client.wrap_socket(raw, server_hostname=server_hostname) as secured:
                    secured.recv(5)
        except (OSError, ValueError) as exc:
            failures.append(type(exc).__name__)
        finally:
            thread.join(timeout=4)
            listener.close()
        return failures

    def test_client_certificate_is_required(self):
        client = ssl.create_default_context(cafile=str(self.profile / "authority" / "ca.crt"))
        self.assertTrue(self._handshake(client))

    def test_wrong_server_hostname_is_rejected(self):
        self.assertTrue(self._handshake(client_context(self.profile / "device"), "wrong-host.invalid"))

    def test_untrusted_client_ca_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / "rogue"
            bootstrap_local_pair(profile, "rogue", {"system.info"})
            client = client_context(profile / "device")
            client.load_verify_locations(cafile=str(self.profile / "authority" / "ca.crt"))
            self.assertTrue(self._handshake(client))

    def test_durable_reservation_crash_does_not_repeat(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "receipts.db"
            store = DurableReceipts(path)
            unknown = {"ok": False, "outcome": "unknown", "secret": "not-readable-in-sqlite"}
            self.assertTrue(store.reserve("id", "hash-only", unknown))
            self.assertFalse(store.reserve("id", "other", {"ok": True}))
            reopened = DurableReceipts(path)
            self.assertEqual(reopened["id"], ("hash-only", unknown))
            self.assertNotIn(b"not-readable-in-sqlite", path.read_bytes())
            self.assertEqual(len(reopened), 1)

    def test_worker_uses_durable_results_without_second_os_call(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = ToolRegistry()
            handler = Mock(return_value={"ok": True, "result": "one execution"})
            registry.register(ToolSpec("info", "", {"properties": {}, "additionalProperties": False}, "system.info"), handler)
            path = Path(folder) / "receipts.db"
            first = DeviceWorker(registry, DurableReceipts(path))
            command = {"version": 1, "operation": "execute", "id": "once", "device_id": first.local.device.device_id,
                       "capability": "system.info", "arguments": {}, "confirmed": False, "deadline": time.time()+20}
            self.assertTrue(first.handle(command)["ok"])
            second = DeviceWorker(registry, DurableReceipts(path))
            self.assertTrue(second.handle(command)["ok"])
            handler.assert_called_once()

    def test_worker_signed_scope_blocks_privileged_capability(self):
        worker = DeviceWorker(allowed_capabilities={"system.info"})
        result = worker.handle({"version": 1, "operation": "execute", "id": "forbidden", "device_id": worker.local.device.device_id,
                                "capability": "app.close", "arguments": {"app": "notepad"}, "confirmed": True, "deadline": time.time()+20})
        self.assertFalse(result["ok"])
        self.assertIn("not granted", result["error"])

    def test_cached_result_does_not_bypass_narrowed_certificate_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "receipts.db"
            worker = DeviceWorker(receipt_store=DurableReceipts(path), allowed_capabilities={"system.info"})
            command = {"version": 1, "operation": "execute", "id": "old-scope", "device_id": worker.local.device.device_id,
                       "capability": "system.info", "arguments": {}, "confirmed": False, "deadline": time.time()+20}
            self.assertTrue(worker.handle(command)["ok"])
            narrowed = DeviceWorker(receipt_store=DurableReceipts(path), allowed_capabilities={"system.battery"})
            self.assertFalse(narrowed.handle(command)["ok"])

    def test_grant_database_failure_closes_transport(self):
        agent = PairedLocalAgent(DeviceRegistry(), self.profile)
        try:
            with patch.object(agent.transport.grants, "get", side_effect=sqlite3.OperationalError("unavailable")):
                self.assertFalse(agent.refresh())
                self.assertFalse(agent.device.online)
        finally:
            agent.close()

    def test_transport_loss_reports_unknown_not_success(self):
        agent = PairedLocalAgent(DeviceRegistry(), self.profile)
        try:
            with patch.object(agent.transport.frame, "receive", side_effect=TimeoutError):
                result = agent.execute("system.info", {}, request_id="lost-reply")
            self.assertFalse(result["ok"])
            self.assertEqual(result["outcome"], "unknown")
            self.assertFalse(agent.device.online)
        finally:
            agent.close()

    def test_cli_factory_uses_paired_mode(self):
        from agentic.cli import create_core
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {"EPIS_DEVICE_TRANSPORT": "paired", "EPIS_PAIRING_DIR": str(self.profile)}), \
             patch("agentic.cli.MemoryManager") as memory:
            memory.return_value.memory_dir = folder
            core = create_core()
            try:
                self.assertIsInstance(core.local_agent, PairedLocalAgent)
                self.assertTrue(core.local_agent.execute("system.info", {})["ok"])
            finally:
                core.close()

    def test_signed_but_unenrolled_client_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / "unenrolled"
            bootstrap_local_pair(profile, "unenrolled", {"system.info"})
            with database(profile / "authority" / "grants.db") as db:
                db.execute("DELETE FROM grants")
            with self.assertRaises(PermissionError):
                PairedLocalAgent(DeviceRegistry(), profile)


if __name__ == "__main__":
    unittest.main()
