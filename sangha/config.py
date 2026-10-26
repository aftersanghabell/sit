"""Configuration: sit.toml at the project root, overridden by CLI flags."""

from __future__ import annotations

import secrets
import tomllib
from dataclasses import dataclass, field, fields
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".sit"

DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net",
    "wss://nostr.mom",
    "wss://relay.nostr.net",
]


@dataclass
class Config:
    subject: str = "peace"
    anchor: str = "The quiet after a bell has stopped ringing."
    bell_seconds: int = 30
    mode: str = "open"
    model: str = "gemma3:4b"
    embed_model: str | None = "nomic-embed-text"
    ollama_host: str = "http://127.0.0.1:11434"  # not "localhost": ::1 hangs on Windows
    num_gpu: int = -1  # layers to put on the GPU; -1 lets Ollama decide, 0 forces CPU
    drift_floor: float = 0.40  # cosine to the anchor below which the mind is told to return
    rumination_ceiling: float = 0.85  # cosine to a recent breath above which it is told to let go
    relays: list[str] = field(default_factory=lambda: list(DEFAULT_RELAYS))
    journal_every: int = 20
    max_breath_chars: int = 200
    room_url: str = ""
    max_breaths: int = 0  # 0 = until stopped
    sit_id: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or ROOT / "sit.toml"
        cfg = cls()
        if path.exists():
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            names = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in names:
                    setattr(cfg, k, v)
        if cfg.embed_model in ("", "none"):
            cfg.embed_model = None
        return cfg

    def new_sit_id(self) -> str:
        return f"{self.subject.replace(' ', '-')}-{date.today():%Y%m%d}-{secrets.token_hex(2)}"
