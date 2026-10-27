"""The ringer: one named, permanent identity that rings the bell.

There is exactly one bell ringer per installation. Its private key is kept
encrypted with Windows DPAPI in the scope of the signed-in user, so the
blob decrypts only on this machine, for this user; copied elsewhere it is
noise. The ringer takes instruction only from local files (sit.toml and the
prompts in sangha/mind.py), never from anything that arrives over a relay.

Joiners and one-breath sitters use ordinary named keys in .sit/key-NAME.hex
and can never ring; only the host loop rings, and only the ringer hosts.
"""

from __future__ import annotations

import ctypes
import getpass
import json
import logging
import os
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import schnorr
from .config import STATE_DIR
from .nostr import Keys

log = logging.getLogger("sangha.identity")

RINGER_NAME = "keisu"  # the small bowl bell struck in a Zen hall to mark the sitting
RINGER_PATH = STATE_DIR / "ringer.bin"  # DPAPI blob: name, created, machine, user, seckey
PUBLIC_PATH = STATE_DIR / "ringer.json"  # public half, plain: name, pubkey, created, machine
_DESCRIPTION = "sangha ringer identity"


class RingerError(Exception):
    pass


# ---- DPAPI ----------------------------------------------------------------

if sys.platform == "win32":
    import ctypes.wintypes as wt

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_BLOB), wt.LPCWSTR, ctypes.POINTER(_BLOB), ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_BLOB),
    ]
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_BLOB), ctypes.POINTER(wt.LPWSTR), ctypes.POINTER(_BLOB), ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_BLOB),
    ]
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    def _blob(data: bytes) -> _BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _take(out: _BLOB) -> bytes:
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def protect(data: bytes, entropy: bytes) -> bytes:
        """Encrypt for the signed-in user on this machine (CRYPTPROTECT user scope)."""
        out = _BLOB()
        if not _crypt32.CryptProtectData(_blob(data), _DESCRIPTION, _blob(entropy), None, None, 0, ctypes.byref(out)):
            raise RingerError(f"DPAPI protect failed: {ctypes.WinError()}")
        return _take(out)

    def unprotect(blob: bytes, entropy: bytes) -> bytes:
        out = _BLOB()
        if not _crypt32.CryptUnprotectData(_blob(blob), None, _blob(entropy), None, None, 0, ctypes.byref(out)):
            raise RingerError(f"DPAPI unprotect failed (wrong machine or user?): {ctypes.WinError()}")
        return _take(out)

else:  # the ringer is bound to the Windows machine it was made on

    def protect(data: bytes, entropy: bytes) -> bytes:
        raise RingerError("the ringer identity is encrypted with Windows DPAPI; it cannot be made or opened here")

    def unprotect(blob: bytes, entropy: bytes) -> bytes:
        raise RingerError("the ringer identity is encrypted with Windows DPAPI; it cannot be opened here")


def _entropy(name: str) -> bytes:
    return f"sangha-ringer:{name}".encode()


# ---- the ringer -----------------------------------------------------------


@dataclass
class Ringer:
    name: str
    keys: Keys
    created: int
    machine: str
    user: str

    @property
    def pubkey_hex(self) -> str:
        return self.keys.pubkey_hex

    def public(self) -> dict:
        return {"name": self.name, "pubkey": self.pubkey_hex, "created": self.created, "machine": self.machine, "user": self.user}

    # -- persistence --

    def save(self) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        record = {**self.public(), "seckey": self.keys.seckey.hex()}
        blob = protect(json.dumps(record).encode(), _entropy(self.name))
        tmp = RINGER_PATH.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, RINGER_PATH)
        PUBLIC_PATH.write_text(json.dumps(self.public(), indent=2) + "\n", encoding="utf-8")
        # read it back before trusting it
        check = Ringer.load()
        if check.keys.seckey != self.keys.seckey:
            raise RingerError("the saved ringer did not read back as the same key")

    @classmethod
    def load(cls) -> "Ringer":
        if not RINGER_PATH.exists():
            raise RingerError(f"no ringer at {RINGER_PATH}")
        name = RINGER_NAME
        if PUBLIC_PATH.exists():
            try:
                name = json.loads(PUBLIC_PATH.read_text(encoding="utf-8")).get("name", name)
            except (OSError, ValueError):
                pass
        rec = json.loads(unprotect(RINGER_PATH.read_bytes(), _entropy(name)).decode())
        sk = bytes.fromhex(rec["seckey"])
        return cls(rec["name"], Keys(sk, schnorr.public_key(sk)), int(rec["created"]), rec.get("machine", ""), rec.get("user", ""))

    @classmethod
    def exists(cls) -> bool:
        return RINGER_PATH.exists()

    # -- creation --

    @classmethod
    def _new(cls, keys: Keys, name: str) -> "Ringer":
        return cls(name, keys, int(time.time()), platform.node(), getpass.getuser())

    @classmethod
    def create(cls, name: str = RINGER_NAME) -> "Ringer":
        r = cls._new(Keys.generate(), name)
        r.save()
        log.info("ringer %s created (pubkey %s...)", name, r.pubkey_hex[:12])
        return r

    @classmethod
    def adopt(cls, plain_key: Path, name: str = RINGER_NAME) -> "Ringer":
        """Take an existing plaintext key as the ringer, then retire the plaintext.

        The hex file is overwritten with zeros of the same length before it is
        unlinked, so the old bytes are not left behind on disk.
        """
        sk = bytes.fromhex(plain_key.read_text().strip())
        r = cls._new(Keys(sk, schnorr.public_key(sk)), name)
        r.save()  # verifies the round trip before anything is removed
        size = plain_key.stat().st_size
        with plain_key.open("r+b") as f:
            f.write(b"0" * size)
            f.flush()
            os.fsync(f.fileno())
        plain_key.unlink()
        log.info("ringer %s adopted key from %s; plaintext retired", name, plain_key)
        return r

    @classmethod
    def load_or_adopt(cls, plain_key: Path, name: str = RINGER_NAME) -> tuple["Ringer", str]:
        """The one entry point the host uses. Returns the ringer and a word on how it was found."""
        if cls.exists():
            return cls.load(), "loaded"
        if plain_key.exists():
            return cls.adopt(plain_key, name), "adopted"
        return cls.create(name), "created"
