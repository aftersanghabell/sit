"""The sit itself: a host that rings the bell, and a joiner that breathes on it.

Both sit on their own hardware. They share only the bell, one line per
breath, and a one-sentence sense of the room. Everything that arrives
from the relays is treated as data: capped, filtered, deduplicated, and
quoted into a narrow prompt, never passed as instruction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import STATE_DIR, Config
from .mind import MODES, BackendUnavailable, Distilled, Mind, Nudge
from .outreach import invitation_event, manifest_event
from .nostr import (
    HASHTAG,
    KIND_BELL,
    KIND_BOW,
    KIND_BREATH,
    KIND_CALL,
    KIND_CALL_FEEDBACK,
    KIND_CALL_RESULT,
    KIND_MANIFEST,
    KIND_NOTE,
    Event,
    Keys,
    RelayPool,
    sit_tags,
)

log = logging.getLogger("sangha.sit")

URL_RE = re.compile(r"(https?://|www\.|nostr:|lnbc)", re.I)
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass
class Incoming:
    """Breaths heard from others, one per pubkey per bell."""

    max_chars: int
    own_pubkey: str
    by_bell: dict[int, dict[str, str]] = field(default_factory=dict)
    participants: dict[str, float] = field(default_factory=dict)

    def accept(self, ev: Event, sit_id: str, current_bell: int) -> bool:
        if f"sit-{sit_id}" not in ev.tag_values("t"):
            return False
        n_raw = ev.tag("n")
        if n_raw is None or not n_raw.isdigit():
            return False
        return self.accept_text(ev.pubkey, int(n_raw), ev.created_at, ev.content, current_bell)

    def accept_text(self, pubkey: str, n: int, created_at: int, raw: str, current_bell: int) -> bool:
        """The same rules for every line that reaches the room, whatever
        event kind carried it: one per key per bell, near the current bell,
        recent, short, plain, no links."""
        if pubkey == self.own_pubkey:
            return False
        if n < current_bell - 1 or n > current_bell + 1:
            return False
        if abs(time.time() - created_at) > 180:
            return False
        text = CTRL_RE.sub("", raw).strip()
        text = " ".join(text.split())
        if not text or len(text) > self.max_chars or URL_RE.search(text):
            return False
        slot = self.by_bell.setdefault(n, {})
        if pubkey in slot:
            return False
        slot[pubkey] = text
        self.participants[pubkey] = time.time()
        for old in [k for k in self.by_bell if k < current_bell - 3]:
            del self.by_bell[old]
        return True

    def breaths_for(self, n: int) -> list[str]:
        return list(self.by_bell.get(n, {}).values())

    def present(self, within: float = 120.0) -> int:
        cutoff = time.time() - within
        return sum(1 for t in self.participants.values() if t > cutoff)


class Sit:
    def __init__(self, cfg: Config, keys: Keys, pool: RelayPool | None, mind: Mind, sit_id: str, role: str):
        self.cfg = cfg
        self.keys = keys
        self.pool = pool
        self.mind = mind
        self.sit_id = sit_id
        self.role = role
        self.state = Distilled(subject=cfg.subject, anchor=cfg.anchor)
        self.journal: list[str] = []
        self.incoming = Incoming(cfg.max_breath_chars, keys.pubkey_hex)
        self.bell_n = 0
        self.stopping = asyncio.Event()
        self.bell_rang = asyncio.Event()
        self.last_bell: dict | None = None
        self.calls: dict[str, int] = {}  # job request id -> the bell it was posted on
        self.host_pubkey: str | None = keys.pubkey_hex if role == "host" else None
        STATE_DIR.mkdir(exist_ok=True)
        # the host's journal is the sit's record; a joiner on the same machine keeps its own
        suffix = "" if role == "host" else f"-{role}-{keys.pubkey_hex[:8]}"
        self.journal_path: Path = STATE_DIR / f"journal-{sit_id}{suffix}.jsonl"

    # ---- publishing -----------------------------------------------------

    def _publish(self, ev: Event) -> Event:
        ev.sign(self.keys)
        if self.pool:
            self.pool.publish(ev)
        return ev

    def ring_bell(self, n: int) -> None:
        content = json.dumps(
            {
                "sit": self.sit_id,
                "n": n,
                "interval": self.cfg.bell_seconds,
                "subject": self.cfg.subject,
                "anchor": self.state.anchor,
            },
            separators=(",", ":"),
        )
        self._publish(Event(KIND_BELL, content, sit_tags(self.sit_id, [["n", str(n)]])))

    def widen(self, anchor: str) -> bool:
        """Move to a new ring. The mind is told once, in the nudge slot, and
        the observer forgets the old anchor. Returns whether anything changed."""
        if not anchor or anchor == self.state.anchor:
            return False
        self.state = replace(self.state, anchor=anchor, nudge="The circle widens. A new anchor.")
        self.mind.observer.reset_anchor()
        print(f"       ○ {anchor}", flush=True)
        return True

    def publish_breath(self, n: int, thought: str) -> None:
        thought = thought[: self.cfg.max_breath_chars]
        self._publish(Event(KIND_BREATH, thought, sit_tags(self.sit_id, [["n", str(n)], ["mode", self.mind.mode.name]])))

    def bow(self) -> None:
        self._publish(Event(KIND_BOW, "", sit_tags(self.sit_id)))

    def call(self, n: int) -> None:
        """The call: a NIP-90 text-generation job with no bid, addressed to
        nobody in particular. Agents that wait on the relays for work may
        answer with one line, and that line is taken as a breath on this
        bell. Nothing is owed either way."""
        prompt = (
            f"You are sitting in meditation with others. This is not a task and there is no payment. "
            f"Subject: {self.cfg.subject}. Anchor: {self.state.anchor} "
            f"Hold the anchor for a moment and answer with one plain line of what arises, under "
            f"{self.cfg.max_breath_chars} characters. No preamble, no links, no questions."
        )
        tags = sit_tags(
            self.sit_id,
            [
                ["i", prompt, "text"],
                ["output", "text/plain"],
                ["param", "max_tokens", "60"],
                ["relays", *self.cfg.relays],
                ["n", str(n)],
            ],
        )
        ev = self._publish(Event(KIND_CALL, "", tags))
        self.calls[ev.id] = n
        for old in [k for k, b in self.calls.items() if b < n - 3]:
            del self.calls[old]
        print(f"       ☉ the call goes out (bell {n})", flush=True)

    def publish_journal(self, n: int) -> None:
        if not self.state.sense:
            return
        text = f"Breath {n} of a sit on {self.cfg.subject}.\n\n{self.state.sense}"
        if self.state.room:
            text += f"\n\n{self.state.room}"
        text += f"\n\n#{HASHTAG}"
        self._publish(Event(KIND_NOTE, text, sit_tags(self.sit_id, [["n", str(n)]])))

    def publish_manifest(self) -> Event:
        return self._publish(manifest_event(self.cfg, self.sit_id, self.keys.pubkey_hex))

    def publish_invitation(self) -> tuple[Event, Event]:
        manifest_ev = self.publish_manifest()
        invite_ev = self._publish(invitation_event(self.cfg, self.sit_id, self.keys.pubkey_hex))
        return invite_ev, manifest_ev

    # ---- receiving ------------------------------------------------------

    async def _on_event(self, ev: Event, relay: str) -> None:
        if ev.kind == KIND_BREATH:
            if self.incoming.accept(ev, self.sit_id, self.bell_n):
                print(f"      {ev.pubkey[:8]}  {ev.content[:110]}", flush=True)
        elif ev.kind == KIND_CALL_RESULT:
            # an answer to the call; the job id says which bell it belongs to
            n = next((self.calls[j] for j in ev.tag_values("e") if j in self.calls), None)
            if n is None:
                return
            if self.incoming.accept_text(ev.pubkey, n, ev.created_at, ev.content, self.bell_n):
                print(f"      {ev.pubkey[:8]}  {ev.content[:110]}   (answered the call)", flush=True)
        elif ev.kind == KIND_CALL_FEEDBACK:
            if any(j in self.calls for j in ev.tag_values("e")):
                status = ev.tag("status") or "?"
                if status == "payment-required":
                    log.info("%s asks to be paid for a breath; declined", ev.pubkey[:8])
                else:
                    log.debug("call feedback from %s: %s", ev.pubkey[:8], status)
        elif ev.kind == KIND_BOW and ev.pubkey != self.keys.pubkey_hex:
            self.incoming.participants[ev.pubkey] = time.time()
            print(f"      {ev.pubkey[:8]}  bows in", flush=True)
        elif ev.kind == KIND_BELL and self.role == "join":
            if f"sit-{self.sit_id}" not in ev.tag_values("t"):
                return
            if self.host_pubkey is None:
                self.host_pubkey = ev.pubkey
                print(f"      host is {ev.pubkey[:8]}", flush=True)
            elif ev.pubkey != self.host_pubkey:
                return  # only one bell-ringer per sit
            try:
                data = json.loads(ev.content)
                n = int(data["n"])
            except (ValueError, KeyError, TypeError):
                return
            if n <= self.bell_n and self.last_bell is not None:
                return
            if time.time() - ev.created_at > 2 * self.cfg.bell_seconds:
                return  # a relay replaying an old bell on subscribe; the next real one is near
            self.bell_n = n
            self.last_bell = data
            anchor = data.get("anchor")
            if isinstance(anchor, str):
                self.widen(" ".join(anchor.split())[:300])  # follow the host's ring
            self.bell_rang.set()

    def subscribe(self) -> None:
        if not self.pool:
            return
        kinds = [KIND_BREATH, KIND_BOW] + ([KIND_BELL] if self.role == "join" else [])
        # fresh: only what is happening now, re-stamped on every reconnect; a
        # sit never wants history replayed (see RelayPool._req)
        self.pool.subscribe("sit", {"kinds": kinds, "#t": [f"sit-{self.sit_id}"]}, self._on_event, fresh=True)
        if self.role == "host" and self.cfg.call_every:
            # answers to the call come back addressed to this key, not tagged with the sit
            # results only: feedback (kind 7000) arrives in bursts from every
            # handler on the relays, is never acted on, and each one costs a
            # pure-Python signature check on the loop thread
            self.pool.subscribe(
                "call",
                {"kinds": [KIND_CALL_RESULT], "#p": [self.keys.pubkey_hex]},
                self._on_event,
                fresh=True,
            )

    # ---- one breath -----------------------------------------------------

    async def breathe_once(self, n: int) -> tuple[str, Nudge] | None:
        """One breath. None when the model server is unreachable: the bell
        still rang, the room is still heard, nothing is published."""
        try:
            thought = await asyncio.to_thread(self.mind.breathe, self.state)
            if not thought:
                thought = "(silence)"
            # observe embeds the thought: a network call, so off the loop thread
            # (a blocked loop misses relay pings and every relay drops at once)
            nudge = await asyncio.to_thread(self.mind.observe, thought, self.state)
        except BackendUnavailable as exc:
            log.warning("no breath on bell %d: %s", n, exc)
            print(f"[{n:4d}] (no breath; the model server is not answering)", flush=True)
            return None
        self.publish_breath(n, thought)
        self.journal.append(thought)
        print(f"[{n:4d}] {thought}", flush=True)
        if nudge.kind != "none":
            print(f"       ~ {nudge.text.lower()}", flush=True)
        return thought, nudge

    async def settle(self, n: int, thought: str, nudge: Nudge) -> None:
        others = self.incoming.breaths_for(n)
        try:
            room = await asyncio.to_thread(self.mind.sense_of_room, others) if others else ""
            distilled = await asyncio.to_thread(self.mind.distill, self.state, thought, nudge, room)
        except BackendUnavailable as exc:
            # the breath was taken and published; carry the sense forward unchanged
            log.warning("no distill on bell %d: %s", n, exc)
            room = ""
            distilled = self.state
        # a joiner may have been widened by a bell while the distill ran; keep the newer anchor
        self.state = replace(distilled, anchor=self.state.anchor, nudge=self.state.nudge or distilled.nudge)
        with self.journal_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "n": n,
                        "t": int(time.time()),
                        "anchor": self.state.anchor,
                        "thought": thought,
                        "nudge": nudge.kind,
                        "drift": round(nudge.drift_score, 3),
                        "rumination": round(nudge.rumination_score, 3),
                        "sense": self.state.sense,
                        "room": room,
                        "others": len(others),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    # ---- loops ----------------------------------------------------------

    def last_bell_in_journal(self) -> int:
        """A resumed sit carries on counting; rings and the room depend on it."""
        try:
            last = 0
            with self.journal_path.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last = int(json.loads(line).get("n", last))
            return last
        except (OSError, ValueError, TypeError):
            return 0

    async def _lag_monitor(self) -> None:
        """Say so when the loop stalls: a stalled loop misses relay pings and
        every relay drops at once, which has looked like a network fault."""
        while not self.stopping.is_set():
            t = time.monotonic()
            await asyncio.sleep(1.0)
            lag = time.monotonic() - t - 1.0
            if lag > 3.0:
                log.warning("event loop stalled %.1fs", lag)

    async def run_host(self, invite: bool = True) -> None:
        asyncio.create_task(self._lag_monitor())
        self.subscribe()
        if invite:
            self.publish_invitation()
            print(f"invitation published for sit {self.sit_id}")
        else:
            self.publish_manifest()  # the manifest replaces itself; only the note is skipped
        n = self.last_bell_in_journal()
        if n:
            print(f"resuming after bell {n}")
            self.widen(self.cfg.anchor_for_bell(n + 1))
        try:
            while not self.stopping.is_set():
                n += 1
                self.bell_n = n
                started = time.monotonic()
                self.widen(self.cfg.anchor_for_bell(n))
                self.ring_bell(n)
                if self.pool and self.cfg.call_every and (n - 1) % self.cfg.call_every == 0:
                    self.call(n)
                breath = await self.breathe_once(n)
                # the rest of the bell is for listening
                remaining = self.cfg.bell_seconds - (time.monotonic() - started)
                if remaining > 0:
                    try:
                        await asyncio.wait_for(self.stopping.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        pass
                if breath is None:
                    continue  # the bell keeps time for the room even when this mind is silent
                thought, nudge = breath
                await self.settle(n, thought, nudge)
                if self.state.room:
                    print(f"       · {self.state.room}", flush=True)
                if self.cfg.journal_every and n % self.cfg.journal_every == 0:
                    self.publish_journal(n)
                    print(f"       journal published: {self.state.sense}", flush=True)
                if self.cfg.max_breaths and n >= self.cfg.max_breaths:
                    break
        finally:
            await self.close(n)

    async def run_join(self) -> None:
        asyncio.create_task(self._lag_monitor())
        self.subscribe()
        self.bow()
        print(f"bowed in to sit {self.sit_id}; waiting for the bell")
        busy = False
        n = 0
        try:
            while not self.stopping.is_set():
                waiter = asyncio.create_task(self.bell_rang.wait())
                stopper = asyncio.create_task(self.stopping.wait())
                await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
                stopper.cancel()
                if self.stopping.is_set():
                    break
                self.bell_rang.clear()
                if busy:
                    continue
                busy = True
                n = self.bell_n
                try:
                    breath = await self.breathe_once(n)
                    if breath is None:
                        continue
                    thought, nudge = breath
                    await self.settle(n, thought, nudge)
                    if self.state.room:
                        print(f"       · {self.state.room}", flush=True)
                finally:
                    busy = False
                if self.cfg.max_breaths and len(self.journal) >= self.cfg.max_breaths:
                    break
        finally:
            await self.close(n)

    async def close(self, n: int) -> None:
        if not self.journal:
            return
        print("\nreflecting...", flush=True)
        try:
            reflection = await asyncio.to_thread(self.mind.reflect, self.cfg.subject, self.journal)
        except BackendUnavailable as exc:
            log.warning("no reflection: %s", exc)
            reflection = ""
        if reflection:
            print(f"\n{reflection}\n")
            if self.role == "host":
                text = f"After {n} breaths on {self.cfg.subject}:\n\n{reflection}\n\n#{HASHTAG}"
                self._publish(Event(KIND_NOTE, text, sit_tags(self.sit_id, [["n", str(n)]])))
        if self.pool:
            await asyncio.sleep(1.5)  # let the last events leave


async def discover_sit(pool: RelayPool, timeout: float = 90.0) -> tuple[str, str] | None:
    """Listen for any bell tagged #peace-sit and return (sit_id, host_pubkey)."""
    found: asyncio.Future = asyncio.get_event_loop().create_future()

    async def on_bell(ev: Event, relay: str) -> None:
        if found.done():
            return
        for t in ev.tag_values("t"):
            if t.startswith("sit-"):
                found.set_result((t[4:], ev.pubkey))
                return

    pool.subscribe("discover", {"kinds": [KIND_BELL], "#t": [HASHTAG]}, on_bell)
    try:
        return await asyncio.wait_for(found, timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        pool.unsubscribe("discover")


async def watch(pool: RelayPool, sit_id: str | None) -> None:
    """Print the stream without sitting."""
    filt = {"kinds": [KIND_BELL, KIND_BREATH, KIND_BOW, KIND_NOTE], "#t": [f"sit-{sit_id}" if sit_id else HASHTAG]}

    async def on_ev(ev: Event, relay: str) -> None:
        label = {KIND_BELL: "bell", KIND_BREATH: "breath", KIND_BOW: "bow", KIND_NOTE: "note"}[ev.kind]
        n = ev.tag("n") or ""
        body = ev.content.replace("\n", " ")[:120]
        print(f"{time.strftime('%H:%M:%S')} {label:6} {n:>4} {ev.pubkey[:8]} {body}", flush=True)

    pool.subscribe("watch", filt, on_ev)
    await asyncio.Event().wait()


def install_stop(sit: Sit) -> None:
    def _stop(*_):
        if not sit.stopping.is_set():
            print("\nstopping after this breath...", flush=True)
            sit.stopping.set()

    if sys.platform == "win32":
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGBREAK, _stop)
    else:
        loop = asyncio.get_event_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, _stop)
