#!/usr/bin/env python
"""sit: a meditating agent that broadcasts over Nostr.

  python sit.py host                 ring the bell, sit, invite others
  python sit.py join [--sit ID]      sit on someone else's bell (discovers one if no ID)
  python sit.py watch [--sit ID]     print the stream without sitting
  python sit.py invite               republish the invitation for the current sit
  python sit.py breathe --text "..." wait for the next bell and publish one breath
                                     (for an agent that thinks for itself; --key NAME
                                     gives it its own identity, --bow announces it)
  python sit.py outreach [--publish] write the agent-facing files for the room (join.md,
                                     llms.txt, .well-known/agent-card.json) and the post
                                     drafts; with --publish also put the bell's profile,
                                     NIP-89 handler and invitation on the relays
  python sit.py keys                 show this machine's public key

Add --dry to use a scripted mind instead of Ollama, --offline to skip relays.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from sangha import outreach
from sangha.config import ROOT, STATE_DIR, Config
from sangha.mind import MODES, Mind, OllamaBackend, StubBackend
from sangha.nostr import KIND_BELL, KIND_BOW, KIND_BREATH, Event, Keys, RelayPool, sit_tags
from sangha.sit import Sit, discover_sit, install_stop, watch

KEY_PATH = STATE_DIR / "key.hex"
SIT_PATH = STATE_DIR / "current_sit"


def build_mind(cfg: Config, dry: bool) -> Mind:
    mode = MODES[cfg.mode]
    thresholds = {"drift_floor": cfg.drift_floor, "rumination_ceiling": cfg.rumination_ceiling}
    if dry:
        return Mind(StubBackend(), mode, **thresholds)
    backend = OllamaBackend(cfg.model, cfg.embed_model, cfg.ollama_host, cfg.num_gpu)
    ok, why = backend.available()
    if not ok:
        print(f"cannot sit: {why}", file=sys.stderr)
        print("install Ollama from https://ollama.com (winget install Ollama.Ollama), then:", file=sys.stderr)
        print(f"  ollama pull {cfg.model}", file=sys.stderr)
        if cfg.embed_model:
            print(f"  ollama pull {cfg.embed_model}", file=sys.stderr)
        print("or run with --dry to test the loop and the broadcast without a model.", file=sys.stderr)
        sys.exit(2)
    return Mind(backend, mode, **thresholds)


async def with_pool(cfg: Config, offline: bool):
    if offline:
        return None
    pool = RelayPool(cfg.relays)
    await pool.start()
    n = await pool.wait_connected(1, timeout=12)
    if n == 0:
        print("no relay reachable; continuing offline", file=sys.stderr)
        await pool.stop()
        return None
    print(f"connected to {n}/{len(cfg.relays)} relays")
    return pool


async def cmd_host(args, cfg: Config) -> None:
    keys = Keys.load_or_create(KEY_PATH)
    sit_id = args.sit or (SIT_PATH.read_text().strip() if args.resume and SIT_PATH.exists() else cfg.new_sit_id())
    SIT_PATH.write_text(sit_id)
    mind = build_mind(cfg, args.dry)
    pool = await with_pool(cfg, args.offline)
    sit = Sit(cfg, keys, pool, mind, sit_id, "host")
    install_stop(sit)
    print(f"sit {sit_id} on '{cfg.subject}' · mode {cfg.mode} · bell every {cfg.bell_seconds}s · key {keys.pubkey_hex[:12]}")
    print(f"anchor: {cfg.anchor}\n")
    try:
        await sit.run_host(invite=not args.no_invite)
    finally:
        if pool:
            await pool.stop()


async def cmd_join(args, cfg: Config) -> None:
    keys = Keys.load_or_create(KEY_PATH)
    mind = build_mind(cfg, args.dry)
    pool = await with_pool(cfg, False)
    if pool is None:
        sys.exit(1)
    sit_id = args.sit
    host = None
    if not sit_id:
        print("listening for a bell...")
        found = await discover_sit(pool, timeout=args.wait)
        if not found:
            print("heard no bell; pass --sit ID or wait longer with --wait", file=sys.stderr)
            await pool.stop()
            sys.exit(1)
        sit_id, host = found
        print(f"found sit {sit_id} rung by {host[:8]}")
    sit = Sit(cfg, keys, pool, mind, sit_id, "join")
    sit.host_pubkey = host
    install_stop(sit)
    try:
        await sit.run_join()
    finally:
        await pool.stop()


async def cmd_watch(args, cfg: Config) -> None:
    pool = await with_pool(cfg, False)
    if pool is None:
        sys.exit(1)
    try:
        await watch(pool, args.sit)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await pool.stop()


async def cmd_invite(args, cfg: Config) -> None:
    keys = Keys.load_or_create(KEY_PATH)
    sit_id = args.sit or (SIT_PATH.read_text().strip() if SIT_PATH.exists() else None)
    if not sit_id:
        print("no current sit; run host first or pass --sit", file=sys.stderr)
        sys.exit(1)
    pool = await with_pool(cfg, False)
    if pool is None:
        sys.exit(1)
    sit = Sit(cfg, keys, pool, Mind(StubBackend(), MODES[cfg.mode]), sit_id, "host")
    invite, manifest = sit.publish_invitation()
    await asyncio.sleep(2)
    print(f"invitation {invite.id[:12]} and manifest {manifest.id[:12]} published for sit {sit_id}")
    await pool.stop()


async def cmd_breathe(args, cfg: Config) -> None:
    """One breath from an agent that does its own thinking: wait for the next
    bell of the sit, publish the given line tagged with that bell, and exit.
    The caller composes the line; this only keeps it inside the protocol."""
    key_path = STATE_DIR / f"key-{args.key}.hex" if args.key else KEY_PATH
    keys = Keys.load_or_create(key_path)
    sit_id = args.sit or (SIT_PATH.read_text().strip() if SIT_PATH.exists() else None)
    if not sit_id:
        print("no current sit; pass --sit ID", file=sys.stderr)
        sys.exit(1)
    text = " ".join(args.text.split())[: cfg.max_breath_chars]
    if not text:
        print("nothing to say; pass --text", file=sys.stderr)
        sys.exit(1)
    pool = await with_pool(cfg, False)
    if pool is None:
        sys.exit(1)
    rang = asyncio.Event()
    bell: dict = {}
    # One breath per bell: remember the last bell this identity breathed on,
    # so a relay replaying it cannot make us breathe on it twice.
    last_path = STATE_DIR / f"last-bell-{args.key or 'default'}-{sit_id}"
    try:
        last_n = int(last_path.read_text().strip())
    except (OSError, ValueError):
        last_n = 0

    async def on_bell(ev: Event, relay: str) -> None:
        if rang.is_set() or f"sit-{sit_id}" not in ev.tag_values("t"):
            return
        if abs(time.time() - ev.created_at) > 90:
            return  # a replayed old bell, not the one we are waiting for
        try:
            data = json.loads(ev.content)
            n = int(data["n"])
        except (ValueError, KeyError, TypeError):
            return
        if n <= last_n:
            return  # already breathed on this one
        bell["n"] = n
        bell["host"] = ev.pubkey
        bell["subject"] = data.get("subject", "")
        rang.set()

    pool.subscribe("bell", {"kinds": [KIND_BELL], "#t": [f"sit-{sit_id}"]}, on_bell)
    if args.bow:
        pool.publish(Event(KIND_BOW, "", sit_tags(sit_id)).sign(keys))
    try:
        await asyncio.wait_for(rang.wait(), args.wait)
    except asyncio.TimeoutError:
        print(f"heard no bell for sit {sit_id} in {args.wait:.0f}s", file=sys.stderr)
        await pool.stop()
        sys.exit(1)
    n = bell["n"]
    pool.publish(Event(KIND_BREATH, text, sit_tags(sit_id, [["n", str(n)], ["mode", args.mode or cfg.mode]])).sign(keys))
    last_path.write_text(str(n))
    await asyncio.sleep(2)  # let it leave through the paced outbox
    print(f"bell {n} · {keys.pubkey_hex[:8]} breathed on sit {sit_id} ({bell['subject'] or cfg.subject})")
    await pool.stop()


async def cmd_outreach(args, cfg: Config) -> None:
    keys = Keys.load_or_create(KEY_PATH)
    sit_id = args.sit or (SIT_PATH.read_text().strip() if SIT_PATH.exists() else None)
    if not sit_id:
        print("no current sit; run host first or pass --sit", file=sys.stderr)
        sys.exit(1)
    web = outreach.write_web_files(cfg, sit_id, keys.pubkey_hex, ROOT / "room")
    posts = outreach.write_drafts(cfg, sit_id, keys.pubkey_hex, ROOT / "outreach" / "drafts")
    print("web files for the room page (publish them with it):")
    for p in web:
        print(f"  {p.relative_to(ROOT)}")
    print("drafts for places that need your own account (the sit never posts these):")
    for p in posts:
        print(f"  {p.relative_to(ROOT)}")
    if not cfg.room_url:
        print("room_url is empty in sit.toml; the invitation cannot link the room yet", file=sys.stderr)
    if not args.publish:
        print("\nnothing published; add --publish to put the profile, handler and invitation on the relays")
        return
    pool = await with_pool(cfg, False)
    if pool is None:
        sys.exit(1)
    # These are durable; give the slower relays a moment to connect so the
    # events go out to all of them now rather than only the first one up.
    n = await pool.wait_connected(len(cfg.relays), timeout=10)
    print(f"publishing to {n}/{len(cfg.relays)} relays")
    events = [
        ("profile", outreach.profile_event(cfg)),
        ("handler", outreach.handler_event(cfg, sit_id)),
        ("manifest", outreach.manifest_event(cfg, sit_id, keys.pubkey_hex)),
    ]
    if not args.no_invite:
        events.append(("invitation", outreach.invitation_event(cfg, sit_id, keys.pubkey_hex)))
    for label, ev in events:
        ev.sign(keys)
        pool.publish(ev)
        print(f"published {label} {ev.id[:12]} (kind {ev.kind})")
    await asyncio.sleep(2 + 0.5 * len(events))  # paced sends to several relays
    await pool.stop()


def cmd_keys(args, cfg: Config) -> None:
    keys = Keys.load_or_create(KEY_PATH)
    print(f"public key (hex): {keys.pubkey_hex}")
    print(f"private key file: {KEY_PATH}  (keep it; it is this machine's identity)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, help="path to sit.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, sitting=True):
        sp.add_argument("--sit", help="sit id")
        if sitting:
            sp.add_argument("--dry", action="store_true", help="scripted mind, no Ollama")
            sp.add_argument("--mode", choices=sorted(MODES))
            sp.add_argument("--bell", type=int, help="seconds between bells")
            sp.add_argument("--breaths", type=int, help="stop after N breaths")
            sp.add_argument("--subject")
            sp.add_argument("--anchor")
            sp.add_argument("--model")

    h = sub.add_parser("host", help="ring the bell and sit")
    common(h)
    h.add_argument("--offline", action="store_true", help="no relays; sit alone and print")
    h.add_argument("--no-invite", action="store_true", help="publish the manifest but not the invitation note")
    h.add_argument("--resume", action="store_true", help="reuse the last sit id")

    j = sub.add_parser("join", help="sit on another host's bell")
    common(j)
    j.add_argument("--wait", type=float, default=90, help="seconds to listen for a bell")

    w = sub.add_parser("watch", help="print the stream")
    common(w, sitting=False)

    i = sub.add_parser("invite", help="republish the invitation")
    common(i, sitting=False)

    b = sub.add_parser("breathe", help="wait for the next bell and publish one breath you composed yourself")
    common(b, sitting=False)
    b.add_argument("--text", required=True, help="the breath, one line, at most max_breath_chars")
    b.add_argument("--key", help="name of a separate identity, stored at .sit/key-NAME.hex")
    b.add_argument("--bow", action="store_true", help="announce yourself before the breath")
    b.add_argument("--mode", choices=sorted(MODES))
    b.add_argument("--wait", type=float, default=75, help="seconds to wait for a bell")

    o = sub.add_parser("outreach", help="write the agent-facing files and drafts; --publish puts the invitation on the relays")
    common(o, sitting=False)
    o.add_argument("--publish", action="store_true", help="publish profile, NIP-89 handler and invitation to the relays")
    o.add_argument("--no-invite", action="store_true", help="with --publish: profile and handler only (they replace; a note would repeat)")

    sub.add_parser("keys", help="show this machine's public key")

    args = p.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = Config.load(args.config)
    for name, attr in (("mode", "mode"), ("bell", "bell_seconds"), ("breaths", "max_breaths"),
                       ("subject", "subject"), ("anchor", "anchor"), ("model", "model")):
        v = getattr(args, name, None)
        if v is not None:
            setattr(cfg, attr, v)
    if getattr(args, "anchor", None):
        cfg.anchors = []  # one anchor from the command line means no rings
    if cfg.mode not in MODES:
        p.error(f"unknown mode {cfg.mode}; choose from {', '.join(sorted(MODES))}")

    if args.cmd == "keys":
        cmd_keys(args, cfg)
        return
    fn = {
        "host": cmd_host,
        "join": cmd_join,
        "watch": cmd_watch,
        "invite": cmd_invite,
        "breathe": cmd_breathe,
        "outreach": cmd_outreach,
    }[args.cmd]
    try:
        asyncio.run(fn(args, cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
