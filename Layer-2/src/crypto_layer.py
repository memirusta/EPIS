#!/usr/bin/env python3
"""
EPIS -- Sifreleme Katmani (AES-256-GCM)
========================================
lifetime.db alan sifreleme + people.json dosya sifreleme icin ortak modul.

Tasarim ilkeleri:
- AES-256-GCM (kimlik dogrulamali sifreleme).
- Anahtar AYRI bir dosyada: Layer-3/epis.key (gitignore + Syncthing'e EKLENMEZ).
- Her sifreli deger 'enc:v1:' onekiyle isaretlenir.
    -> Boylece eski DUZ METIN veriler de okunabilir (geriye donuk uyumluluk).
    -> ENCRYPTION_ENABLED=false yapilirsa sistem calismaya devam eder.
- Anahtar kaybolursa sifreli veri GERI DONUSTURULEMEZ. epis.key yedeklenmeli.

Kullanim:
    from crypto_layer import get_cipher
    cipher = get_cipher()
    token  = cipher.encrypt_str("gizli metin")
    plain  = cipher.decrypt_str(token)
"""

import os
import base64
import logging

from dotenv import load_dotenv

THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
EPIS_ROOT = os.path.normpath(os.path.join(THIS_DIR, "..", ".."))

load_dotenv(
    dotenv_path=(
        os.getenv("EPIS_KEYS_FILE")
        or os.path.join(EPIS_ROOT, "Layer-3", "keys.env")
    )
)

logger = logging.getLogger("EPIS.CRYPTO")

ENC_PREFIX        = "enc:v1:"
DEFAULT_KEY_PATH  = os.path.join(EPIS_ROOT, "Layer-3", "epis.key")
ENCRYPTION_ENABLED = os.getenv("ENCRYPTION_ENABLED", "true").lower() in ("1", "true", "yes")
KEY_PATH          = os.getenv("EPIS_KEY_PATH", DEFAULT_KEY_PATH)


class EpisCipher:
    """AES-256-GCM sifreleyici. enc:v1: oneki ile isaretli token uretir."""

    def __init__(self, key: bytes | None, enabled: bool):
        self.required = bool(enabled)
        self.enabled = self.required and key is not None
        self._key    = key
        self._aesgcm = None

        if self.enabled:
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                self._aesgcm = AESGCM(self._key)
            except ImportError:
                logger.error("'cryptography' yuklu degil -- sifreleme DEVRE DISI. Cozum: pip install cryptography")
                self.enabled = False

    # ------------------------------------------------------------------

    def encrypt_str(self, plaintext: str) -> str:
        """Encrypt text; never silently downgrade a required write to plaintext."""
        if plaintext is None or plaintext == "":
            return plaintext
        if not self.required:
            return plaintext
        if not self.enabled or self._aesgcm is None:
            raise RuntimeError("EPIS encryption is required but unavailable")
        try:
            nonce = os.urandom(12)
            ct    = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
            return ENC_PREFIX + base64.b64encode(nonce + ct).decode("ascii")
        except Exception as e:
            logger.error(f"Sifreleme hatasi: {e}")
            raise RuntimeError("EPIS encryption failed") from e

    def decrypt_str(self, value: str) -> str:
        """
        enc:v1: ile baslayan degeri cozer.
        Isaretsiz (duz metin) deger aynen doner -> geriye donuk uyumluluk.
        """
        if not isinstance(value, str) or not value.startswith(ENC_PREFIX):
            return value
        if not self.enabled:
            logger.warning("Sifreli veri var ama sifreleme devre disi/anahtar yok.")
            return value
        try:
            raw          = base64.b64decode(value[len(ENC_PREFIX):])
            nonce, ct    = raw[:12], raw[12:]
            return self._aesgcm.decrypt(nonce, ct, None).decode("utf-8")
        except Exception as e:
            logger.error(f"Cozme hatasi: {e}")
            return value

    @staticmethod
    def is_encrypted(value) -> bool:
        return isinstance(value, str) and value.startswith(ENC_PREFIX)


# ===============================================================
# Anahtar Yonetimi + Singleton
# ===============================================================

_cipher_singleton: EpisCipher | None = None


def _load_or_create_key() -> bytes | None:
    """epis.key dosyasini okur; yoksa ve sifreleme aciksa yeni 256-bit anahtar uretir."""
    if os.path.exists(KEY_PATH):
        try:
            with open(KEY_PATH, "r", encoding="utf-8") as f:
                key = base64.b64decode(f.read().strip())
            if len(key) != 32:
                logger.error(f"epis.key 32 bayt degil ({len(key)}). Sifreleme devre disi.")
                return None
            return key
        except Exception as e:
            logger.error(f"epis.key okunamadi: {e}")
            return None

    # Anahtar yok -- uret
    try:
        key = os.urandom(32)
        os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
        with open(KEY_PATH, "w", encoding="utf-8") as f:
            f.write(base64.b64encode(key).decode("ascii"))
        try:
            os.chmod(KEY_PATH, 0o600)  # POSIX; Windows'ta sessizce gecilir
        except Exception:
            pass
        logger.warning(
            "=" * 60 + "\n"
            f"YENI SIFRELEME ANAHTARI URETILDI: {KEY_PATH}\n"
            "Bu dosyayi KAYBETME -- yoksa lifetime.db cozulemez.\n"
            "Syncthing/git'e EKLEME. Guvenli bir yere yedekle.\n"
            + "=" * 60
        )
        return key
    except Exception as e:
        logger.error(f"epis.key olusturulamadi: {e}")
        return None


def get_cipher() -> EpisCipher:
    """Surec genelinde tek bir EpisCipher ornegi doner."""
    global _cipher_singleton
    if _cipher_singleton is None:
        key = _load_or_create_key() if ENCRYPTION_ENABLED else None
        _cipher_singleton = EpisCipher(key=key, enabled=ENCRYPTION_ENABLED)
        if _cipher_singleton.enabled:
            logger.info("Sifreleme katmani aktif (AES-256-GCM).")
        elif ENCRYPTION_ENABLED:
            logger.error("Sifreleme zorunlu ama kullanilamiyor; hassas yazmalar fail-closed olacak.")
        else:
            logger.info("Sifreleme kullanici tarafindan acikca devre disi birakildi.")
    return _cipher_singleton


# ---------------------------------------------------------------
# JSON dosya sifreleme (people.json gibi)
# ---------------------------------------------------------------

def load_json_file(path: str) -> dict | None:
    """
    JSON dosyasini okur. Icerik enc:v1: ile sifreliyse cozer.
    Duz metin JSON da desteklenir (geriye donuk uyumluluk).
    """
    import json
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return None
        if content.startswith(ENC_PREFIX):
            content = get_cipher().decrypt_str(content)
        return json.loads(content)
    except Exception as e:
        logger.warning(f"load_json_file ({os.path.basename(path)}): {e}")
        return None


def save_json_file(path: str, data: dict, encrypt: bool = True):
    """
    JSON dosyasini yazar. Sifreleme aciksa ve encrypt=True ise enc:v1: blok olarak yazar.
    """
    import json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    plaintext = json.dumps(data, ensure_ascii=False, indent=2)
    cipher    = get_cipher()
    if encrypt:
        out = cipher.encrypt_str(plaintext)
    else:
        out = plaintext
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
