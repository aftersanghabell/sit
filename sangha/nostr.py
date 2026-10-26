"""Nostr events and a small relay pool.

Only what the sit needs: build and sign events, publish to several
relays, and subscribe to a filter with a callback. Outbound only; the
laptop never listens on a port.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import websockets

from . import schnorr

log = logging.getLogger("sangha.nostr")

# Event kinds. 20000-29999 are ephemeral in NIP-01: relays forward them
# to live subscribers and do not store them. That suits breath-by-breath
# traffic. Kind 1 is a normal text note and is kept forever.
KIND_NOTE = 1
KIND_MANIFEST = 30078  # app-specific replaceable data, keyed by a "d" tag
KIND_BELL = 20100
KIND_BREATH = 20101
KIND_BOW = 20102  # a participant announcing it has joined

HASHTAG = "peace-sit"


@dataclass
class Keys:
    seckey: bytes
    pubkey: bytes

    @property
    def pubkey_hex(self) -> str:
        return self.pubkey.hex()

    @classmethod
    def generate(cls) -> "Keys":
        sk = schnorr.generate_private_key()
        return cls(sk, schnorr.public_key(sk))

    @classmethod
    def load_or_create(cls, path: Path) -> "Keys":
        if path.exists():
            sk = bytes.fromhex(path.read_text().strip())
            return cls(sk, schnorr.public_key(sk))
        keys = cls.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(keys.seckey.hex())
        log.info("generated new keypair at %s (npub hex %s)", path, keys.pubkey_hex[:16])
        return keys


@dataclass
class Event:
    kind: int
    content: str
    tags: list[list[str]] = field(default_factory=list)
    created_at: int = 0
    pubkey: str = ""
    id: str = ""
    sig: str = ""

    def serialize_for_id(self) -> bytes:
        payload = [0, self.pubkey, self.created_at, self.kind, self.tags, self.content]
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()

    def sign(self, keys: Keys) -> "Event":
        self.pubkey = keys.pubkey_hex
        if not self.created_at:
            self.created_at = int(time.time())
        digest = hashlib.sha256(self.serialize_for_id()).digest()
        self.id = digest.hex()
        self.sig = schnorr.sign(digest, keys.seckey).hex()
        return self

    def verify(self) -> bool:
        try:
            digest = hashlib.sha256(self.serialize_for_id()).digest()
            if digest.hex() != self.id:
                return False
            return schnorr.verify(digest, bytes.fromhex(self.pubkey), bytes.fromhex(self.sig))
        except (ValueError, TypeError):
            return False

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pubkey": self.pubkey,
            "created_at": self.created_at,
            "kind": self.kind,
            "tags": self.tags,
            "content": self.content,
            "sig": self.sig,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Event":
        return cls(
            kind=int(d["kind"]),
            content=str(d.get("content", "")),
            tags=[list(map(str, t)) for t in d.get("tags", [])],
            created_at=int(d["created_at"]),
            pubkey=str(d["pubkey"]),
            id=str(d["id"]),
            sig=str(d["sig"]),
        )

    def tag(self, name: str) -> str | None:
        for t in self.tags:
            if len(t) >= 2 and t[0] == name:
                return t[1]
        return None

    def tag_values(self, name: str) -> list[str]:
        return [t[1] for t in self.tags if len(t) >= 2 and t[0] == name]


Handler = Callable[[Event, str], Awaitable[None]]


class RelayPool:
    """Keeps one websocket per relay, republishes on reconnect, fans out
    subscriptions. Failures on one relay never block the others."""

    def __init__(self, urls: list[str], timeout: float = 8.0):
        self.urls = urls
        self.timeout = timeout
        self._subs: dict[str, tuple[dict[str, Any], Handler]] = {}
        self._tasks: list[asyncio.Task] = []
        self._outbox: dict[str, asyncio.Queue] = {u: asyncio.Queue() for u in urls}
        self._seen: dict[str, float] = {}
        self._connected: set[str] = set()

    @property
    def connected(self) -> list[str]:
        return sorted(self._connected)

    async def start(self) -> None:
        for url in self.urls:
            self._tasks.append(asyncio.create_task(self._run_relay(url), name=f"relay:{url}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def wait_connected(self, min_relays: int = 1, timeout: float = 10.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self._connected) >= min_relays:
                break
            await asyncio.sleep(0.2)
        return len(self._connected)

    def publish(self, event: Event) -> None:
        if not event.sig:
            raise ValueError("publish an unsigned event? sign it first")
        msg = json.dumps(["EVENT", event.to_json()], ensure_ascii=False)
        ephemeral = 20000 <= event.kind < 30000
        for url, q in self._outbox.items():
            # An ephemeral event is only meaningful now. Queueing it for a
            # relay that is down means a burst on reconnect, and relays
            # rate-limit bursts. Durable events are worth waiting for.
            if ephemeral and url not in self._connected:
                continue
            if q.qsize() >= 50:
                continue
            q.put_nowait(msg)

    def subscribe(self, sub_id: str, filt: dict[str, Any], handler: Handler) -> None:
        self._subs[sub_id] = (filt, handler)
        msg = json.dumps(["REQ", sub_id, filt])
        for q in self._outbox.values():
            q.put_nowait(msg)

    def unsubscribe(self, sub_id: str) -> None:
        self._subs.pop(sub_id, None)
        msg = json.dumps(["CLOSE", sub_id])
        for q in self._outbox.values():
            q.put_nowait(msg)

    async def _run_relay(self, url: str) -> None:
        backoff = 2.0
        while True:
            try:
                async with websockets.connect(url, open_timeout=self.timeout, ping_interval=20, max_size=1 << 20) as ws:
                    self._connected.add(url)
                    backoff = 2.0
                    log.info("connected %s", url)
                    # replay live subscriptions on (re)connect
                    for sub_id, (filt, _) in self._subs.items():
                        await ws.send(json.dumps(["REQ", sub_id, filt]))
                    sender = asyncio.create_task(self._sender(ws, url))
                    try:
                        async for raw in ws:
                            await self._handle(raw, url)
                    finally:
                        sender.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - relays fail in many ways
                log.warning("%s: %s", url, exc.__class__.__name__ + (f": {exc}" if str(exc) else ""))
            finally:
                self._connected.discard(url)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _sender(self, ws, url: str) -> None:
        q = self._outbox[url]
        while True:
            msg = await q.get()
            await ws.send(msg)
            await asyncio.sleep(0.4)  # never burst at a relay

    async def _handle(self, raw: str | bytes, url: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, list) or not msg:
            return
        kind = msg[0]
        if kind == "EVENT" and len(msg) >= 3:
            sub_id = msg[1]
            try:
                ev = Event.from_json(msg[2])
            except (KeyError, ValueError, TypeError):
                return
            if not ev.verify():
                log.debug("bad signature from %s", url)
                return
            # de-duplicate across relays; forget ids after ten minutes
            now = time.monotonic()
            if ev.id in self._seen:
                return
            self._seen[ev.id] = now
            if len(self._seen) > 5000:
                cutoff = now - 600
                self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
            sub = self._subs.get(sub_id)
            if sub:
                try:
                    await sub[1](ev, url)
                except Exception:  # noqa: BLE001
                    log.exception("handler failed")
        elif kind == "OK" and len(msg) >= 4 and msg[2] is False:
            log.warning("%s rejected %s: %s", url, str(msg[1])[:8], msg[3])
        elif kind == "NOTICE" and len(msg) >= 2:
            log.info("%s notice: %s", url, msg[1])
        elif kind == "CLOSED" and len(msg) >= 3:
            log.warning("%s closed sub %s: %s", url, msg[1], msg[2])


def sit_tags(sit_id: str, extra: list[list[str]] | None = None) -> list[list[str]]:
    tags = [["t", HASHTAG], ["t", f"sit-{sit_id}"]]
    if extra:
        tags.extend(extra)
    return tags
