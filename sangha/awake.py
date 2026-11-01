"""Keep the machine from idle-sleeping while a sit runs.

A sit is a process that must keep running while nobody touches the laptop,
which is exactly when Windows idle-sleeps it. The first balance sit lost
twenty hours this way: the event loop stalled, Ollama "timed out", every
relay dropped, and the bell fell silent until someone opened the lid.

`hold()` asks Windows (SetThreadExecutionState with ES_SYSTEM_REQUIRED |
ES_CONTINUOUS) to keep the system awake while this process lives; the
display may still turn off. The request belongs to this process alone: it
changes no power setting, it ends when `release()` runs or the process
exits, and a closed lid or a chosen Sleep still sleeps. On any other
platform both calls are no-ops.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("sangha.awake")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_held = False


def hold() -> bool:
    """Keep the system awake until release(). Returns whether the hold took."""
    global _held
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        prev = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception as exc:  # no kernel32, odd runtime: sit anyway
        log.warning("could not ask Windows to stay awake: %s", exc)
        return False
    _held = prev != 0
    if not _held:
        log.warning("Windows refused the stay-awake request; the laptop may idle-sleep during the sit")
    return _held


def release() -> None:
    """Let the system idle-sleep again. Safe to call without a hold."""
    global _held
    if not _held:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass
    _held = False
