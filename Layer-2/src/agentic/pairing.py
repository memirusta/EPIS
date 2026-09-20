"""Offline certificate enrollment and revocable capability grants. Windows MVP."""
from datetime import datetime, timedelta, timezone
from contextlib import closing, contextmanager
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@contextmanager
def database(path):
    # sqlite's transaction context does not close the connection on Windows.
    with closing(sqlite3.connect(path)) as db:
        with db:
            yield db


def protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Device credential storage currently requires Windows DPAPI")
    import win32crypt
    # User scope, UI forbidden. Never use machine-wide decryption scope.
    return win32crypt.CryptProtectData(data, "EPIS device state", None, None, None, 1)


def unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Device credential storage currently requires Windows DPAPI")
    import win32crypt
    return win32crypt.CryptUnprotectData(data, None, None, None, 1)[1]


def save_new(path, data):
    # Enrollment never overwrites an existing identity or key.
    with Path(path).open("xb") as handle:
        handle.write(data)


def _write_key(folder, name, key):
    password = secrets.token_bytes(32)
    protected = protect(password)
    save_new(folder / f"{name}.key", key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password)))
    save_new(folder / f"{name}.password.dpapi", protected)


def _password(folder, name):
    return unprotect((Path(folder) / f"{name}.password.dpapi").read_bytes())


def _load_key(folder, name):
    return serialization.load_pem_private_key((folder / f"{name}.key").read_bytes(), _password(folder, name))


def _name(value):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, value)])


def _certificate(subject, issuer, public_key, issuer_key, *, ca=False, client=False, capabilities=()):
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
               .public_key(public_key).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(minutes=5))
               .not_valid_after(now + timedelta(days=365 if ca else 90))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                   key_encipherment=False, data_encipherment=False, key_agreement=False,
                   key_cert_sign=ca, crl_sign=ca, encipher_only=False, decipher_only=False), critical=True))
    if not ca:
        builder = builder.add_extension(x509.ExtendedKeyUsage([
            ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        if client:
            builder = builder.add_extension(x509.SubjectAlternativeName([
                x509.UniformResourceIdentifier("urn:epis:capability:" + value)
                for value in sorted(capabilities)]), critical=False)
        else:
            builder = builder.add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def create_authority(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        raise ValueError("Authority directory must be empty; refusing identity overwrite")
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _certificate(_name("EPIS local authority"), _name("EPIS local authority"), ca_key.public_key(), ca_key, ca=True)
    server_key = ec.generate_private_key(ec.SECP256R1())
    server = _certificate(_name("EPIS local Core"), ca.subject, server_key.public_key(), ca_key)
    _write_key(folder, "ca", ca_key)
    _write_key(folder, "server", server_key)
    save_new(folder / "ca.crt", ca.public_bytes(serialization.Encoding.PEM))
    save_new(folder / "server.crt", server.public_bytes(serialization.Encoding.PEM))
    GrantStore(folder / "grants.db")


def create_device_request(folder, device_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", device_id):
        raise ValueError("Invalid device ID")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        raise ValueError("Device directory must be empty; refusing identity overwrite")
    key = ec.generate_private_key(ec.SECP256R1())
    csr = x509.CertificateSigningRequestBuilder().subject_name(_name(device_id)).sign(key, hashes.SHA256())
    _write_key(folder, "device", key)
    save_new(folder / "device.csr", csr.public_bytes(serialization.Encoding.PEM))
    save_new(folder / "identity.json", json.dumps({"device_id": device_id}).encode())
    return folder / "device.csr"


class GrantStore:
    def __init__(self, path):
        self.path = str(path)
        with database(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS grants (
                fingerprint TEXT PRIMARY KEY, device_id TEXT UNIQUE NOT NULL,
                capabilities TEXT NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)""")

    def add(self, certificate, device_id, capabilities):
        fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
        with database(self.path) as db:
            db.execute("INSERT INTO grants VALUES (?, ?, ?, ?, 0)",
                       (fingerprint, device_id, json.dumps(sorted(capabilities)),
                        certificate.not_valid_after_utc.timestamp()))
        return fingerprint

    def get(self, fingerprint):
        with database(self.path) as db:
            row = db.execute("SELECT device_id, capabilities, expires, revoked FROM grants WHERE fingerprint=?", (fingerprint,)).fetchone()
        if not row or row[3] or row[2] <= time.time():
            return None
        capabilities = json.loads(row[1])
        if not isinstance(capabilities, list) or any(not isinstance(value, str) for value in capabilities):
            return None
        return {"device_id": row[0], "capabilities": set(capabilities), "expires": row[2]}

    def revoke(self, device_id):
        with database(self.path) as db:
            changed = db.execute("UPDATE grants SET revoked=1 WHERE device_id=?", (device_id,)).rowcount
        return bool(changed)


def approve_request(authority, csr_path, capabilities):
    from .tools import build_local_registry
    capabilities = set(capabilities)
    if not capabilities or not capabilities <= build_local_registry().capabilities("windows"):
        raise ValueError("Only known, explicitly selected Windows capabilities can be granted")
    authority = Path(authority)
    csr = x509.load_pem_x509_csr(Path(csr_path).read_bytes())
    if not csr.is_signature_valid:
        raise ValueError("CSR signature invalid")
    if not isinstance(csr.public_key(), ec.EllipticCurvePublicKey) or not isinstance(csr.public_key().curve, ec.SECP256R1):
        raise ValueError("Device enrollment requires a P-256 public key")
    names = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(names) != 1 or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", names[0].value):
        raise ValueError("CSR device ID invalid")
    ca = x509.load_pem_x509_certificate((authority / "ca.crt").read_bytes())
    certificate = _certificate(csr.subject, ca.subject, csr.public_key(), _load_key(authority, "ca"), client=True, capabilities=capabilities)
    GrantStore(authority / "grants.db").add(certificate, names[0].value, capabilities)
    return certificate.public_bytes(serialization.Encoding.PEM)


def install_device_certificate(folder, certificate, ca_pem):
    folder = Path(folder)
    cert = x509.load_pem_x509_certificate(certificate)
    ca = x509.load_pem_x509_certificate(ca_pem)
    cert.verify_directly_issued_by(ca)
    expected_id = json.loads((folder / "identity.json").read_text())["device_id"]
    if cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value != expected_id:
        raise ValueError("Certificate device ID mismatch")
    if ExtendedKeyUsageOID.CLIENT_AUTH not in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value:
        raise ValueError("Certificate is not a device identity")
    public_format = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if cert.public_key().public_bytes(*public_format) != _load_key(folder, "device").public_key().public_bytes(*public_format):
        raise ValueError("Certificate does not match local device key")
    save_new(folder / "device.crt", certificate)
    save_new(folder / "ca.crt", ca_pem)


def server_context(authority):
    authority = Path(authority)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(authority / "ca.crt"))
    context.load_cert_chain(str(authority / "server.crt"), str(authority / "server.key"), _password(authority, "server"))
    return context


def client_context(device_folder):
    folder = Path(device_folder)
    context = ssl.create_default_context(cafile=str(folder / "ca.crt"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(folder / "device.crt"), str(folder / "device.key"), _password(folder, "device"))
    return context


def certificate_capabilities(certificate):
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    prefix = "urn:epis:capability:"
    return {value[len(prefix):] for value in names.get_values_for_type(x509.UniformResourceIdentifier) if value.startswith(prefix)}


def bootstrap_local_pair(folder, device_id, capabilities):
    """Explicit local demo setup, not automatic enrollment of a remote device."""
    folder = Path(folder)
    if folder.exists():
        raise ValueError("Pairing profile already exists; refusing to overwrite")
    create_authority(folder / "authority")
    csr = create_device_request(folder / "device", device_id)
    certificate = approve_request(folder / "authority", csr, capabilities)
    install_device_certificate(folder / "device", certificate, (folder / "authority" / "ca.crt").read_bytes())


class DurableReceipts:
    """Atomic reservations; DPAPI-encrypted responses survive agent restarts."""
    def __init__(self, path):
        self.path = str(path)
        with database(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result BLOB NOT NULL)")

    def __contains__(self, request_id):
        with database(self.path) as db:
            return db.execute("SELECT 1 FROM receipts WHERE id=?", (request_id,)).fetchone() is not None

    def __len__(self):
        with database(self.path) as db:
            return db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]

    def __getitem__(self, request_id):
        with database(self.path) as db:
            row = db.execute("SELECT fingerprint, result FROM receipts WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise KeyError(request_id)
        return row[0], json.loads(unprotect(row[1]))

    def reserve(self, request_id, fingerprint, result):
        encrypted = protect(json.dumps(result).encode())
        with database(self.path) as db:
            return db.execute("INSERT OR IGNORE INTO receipts VALUES (?, ?, ?)",
                              (request_id, fingerprint, encrypted)).rowcount == 1

    def __setitem__(self, request_id, value):
        fingerprint, result = value
        encrypted = protect(json.dumps(result, ensure_ascii=False).encode())
        with database(self.path) as db:
            updated = db.execute("UPDATE receipts SET result=? WHERE id=? AND fingerprint=?",
                                 (encrypted, request_id, fingerprint)).rowcount
        if not updated:
            raise ValueError("Receipt reservation missing or mismatched")
