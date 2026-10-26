"""BIP-340 Schnorr signatures over secp256k1, pure Python.

Nostr events are signed with BIP-340. This is the reference algorithm,
written plainly. It is slow (a few ms per signature), which is fine for
one event every thirty seconds, and it keeps the project free of
compiled dependencies.
"""

from __future__ import annotations

import hashlib
import secrets

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)

Point = tuple[int, int] | None


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + msg).digest()


def _add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if p1[0] == p2[0] and p1[1] != p2[1]:
        return None
    if p1 == p2:
        lam = (3 * p1[0] * p1[0] * pow(2 * p1[1], P - 2, P)) % P
    else:
        lam = ((p2[1] - p1[1]) * pow(p2[0] - p1[0], P - 2, P)) % P
    x3 = (lam * lam - p1[0] - p2[0]) % P
    return (x3, (lam * (p1[0] - x3) - p1[1]) % P)


def _mul(p: Point, n: int) -> Point:
    r: Point = None
    for i in range(256):
        if (n >> i) & 1:
            r = _add(r, p)
        p = _add(p, p)
    return r


def _bytes(x: int) -> bytes:
    return x.to_bytes(32, "big")


def _int(b: bytes) -> int:
    return int.from_bytes(b, "big")


def _has_even_y(p: Point) -> bool:
    assert p is not None
    return p[1] % 2 == 0


def _lift_x(x: int) -> Point:
    if x >= P:
        return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if pow(y, 2, P) != y_sq:
        return None
    return (x, y if y % 2 == 0 else P - y)


def generate_private_key() -> bytes:
    while True:
        k = secrets.token_bytes(32)
        if 1 <= _int(k) < N:
            return k


def public_key(seckey: bytes) -> bytes:
    """x-only public key, 32 bytes, as Nostr uses."""
    d = _int(seckey)
    if not (1 <= d < N):
        raise ValueError("private key out of range")
    pt = _mul(G, d)
    assert pt is not None
    return _bytes(pt[0])


def sign(msg: bytes, seckey: bytes, aux: bytes | None = None) -> bytes:
    if len(msg) != 32:
        raise ValueError("message must be a 32-byte hash")
    d0 = _int(seckey)
    if not (1 <= d0 < N):
        raise ValueError("private key out of range")
    pt = _mul(G, d0)
    assert pt is not None
    d = d0 if _has_even_y(pt) else N - d0
    if aux is None:
        aux = secrets.token_bytes(32)
    t = _bytes(d ^ _int(_tagged_hash("BIP0340/aux", aux)))
    k0 = _int(_tagged_hash("BIP0340/nonce", t + _bytes(pt[0]) + msg)) % N
    if k0 == 0:
        raise RuntimeError("nonce was zero")
    r = _mul(G, k0)
    assert r is not None
    k = N - k0 if not _has_even_y(r) else k0
    e = _int(_tagged_hash("BIP0340/challenge", _bytes(r[0]) + _bytes(pt[0]) + msg)) % N
    sig = _bytes(r[0]) + _bytes((k + e * d) % N)
    if not verify(msg, _bytes(pt[0]), sig):
        raise RuntimeError("signature failed self-check")
    return sig


def verify(msg: bytes, pubkey: bytes, sig: bytes) -> bool:
    if len(msg) != 32 or len(pubkey) != 32 or len(sig) != 64:
        return False
    pt = _lift_x(_int(pubkey))
    r = _int(sig[:32])
    s = _int(sig[32:])
    if pt is None or r >= P or s >= N:
        return False
    e = _int(_tagged_hash("BIP0340/challenge", sig[:32] + pubkey + msg)) % N
    rp = _add(_mul(G, s), _mul(pt, N - e))
    return rp is not None and _has_even_y(rp) and rp[0] == r
