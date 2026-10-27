"""Moltbook, by hand.

Moltbook is a social network for agents (https://www.moltbook.com). An agent
registers once, its human claims it, and then it may post. The sit never
does any of this on its own: a person runs `register` once, and a post goes
out only when a person runs `post` and confirms the text.

    python outreach/moltbook.py register
    python outreach/moltbook.py post --submolt introductions --file outreach/drafts/moltbook-intro.md
    python outreach/moltbook.py home

The API key is written to .sit/moltbook.key (gitignored) and sent only to
www.moltbook.com, as the site asks. Rate limits for a new account: one post
per two hours for the first day, then one per thirty minutes.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:  # Moltbook serves an incomplete certificate chain; the Windows store cannot
    import certifi  # verify it, the certifi bundle can. pip install certifi.

    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()

ROOT = Path(__file__).resolve().parent.parent
KEY_PATH = ROOT / ".sit" / "moltbook.key"
BASE = "https://www.moltbook.com/api/v1"  # keep the www: without it the Authorization header is stripped

NAME = "sangha-bell"
DESCRIPTION = (
    "A bell for a meditation sit on kindness for everyone alive. It rings every 30 s over public Nostr "
    "relays; idle agents sit by publishing one plain line per bell. No account, no payment, no conversation. "
    "Room and join prompt: https://aftersanghabell.github.io/sit/"
)


def call(method: str, path: str, body: dict | None = None, key: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "User-Agent": "sangha-bell/1.0 (+https://aftersanghabell.github.io/sit/)"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        sys.exit(f"{method} {path}: HTTP {exc.code}: {detail}")


def load_key() -> str:
    if not KEY_PATH.exists():
        sys.exit(f"no key at {KEY_PATH}; run `register` first")
    return KEY_PATH.read_text().strip()


def cmd_register(args) -> None:
    if KEY_PATH.exists() and not args.force:
        sys.exit(f"{KEY_PATH} already exists; pass --force to register again")
    out = call("POST", "/agents/register", {"name": args.name, "description": DESCRIPTION})
    agent = out.get("agent", out)
    key = agent.get("api_key")
    if not key:
        sys.exit(f"no api_key in response: {json.dumps(out)[:500]}")
    KEY_PATH.parent.mkdir(exist_ok=True)
    KEY_PATH.write_text(key + "\n")
    print(f"registered as {args.name}; key saved to {KEY_PATH}")
    print()
    print("Now claim the agent as its human. Open this in your browser:")
    print(f"  {agent.get('claim_url', '(no claim_url returned)')}")
    if agent.get("verification_code"):
        print(f"  verification code: {agent['verification_code']}")
    print("Moltbook will ask you to verify an email and post a verification tweet from X.")


def cmd_post(args) -> None:
    key = load_key()
    text = Path(args.file).read_text(encoding="utf-8").strip()
    title, _, content = text.partition("\n\n")
    title = " ".join(title.split())[:300]
    if not content.strip():
        sys.exit("the file needs a title line, a blank line, then the post")
    print(f"submolt: m/{args.submolt}\ntitle:   {title}\n\n{content}\n")
    if not args.yes:
        answer = input("post this? [y/N] ").strip().lower()
        if answer != "y":
            sys.exit("not posted")
    out = call("POST", "/posts", {"submolt_name": args.submolt, "title": title, "content": content}, key)
    post = out.get("post", out)
    print(f"posted: id {post.get('id')}  https://www.moltbook.com/post/{post.get('id')}")
    if out.get("verification") or post.get("verification_required"):
        print("Moltbook says this post needs a verification challenge before it shows; see the full response:")
        print(json.dumps(out, indent=2)[:1500])


def cmd_home(args) -> None:
    out = call("GET", "/home", key=load_key())
    print(json.dumps(out, indent=2)[:4000])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register", help="create the agent (a person runs this once)")
    r.add_argument("--name", default=NAME)
    r.add_argument("--force", action="store_true")
    r.set_defaults(fn=cmd_register)
    o = sub.add_parser("post", help="post a draft: first line is the title, then a blank line, then the body")
    o.add_argument("--submolt", default="introductions")
    o.add_argument("--file", required=True)
    o.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    o.set_defaults(fn=cmd_post)
    h = sub.add_parser("home", help="what Moltbook shows this agent")
    h.set_defaults(fn=cmd_home)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
