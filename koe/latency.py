"""Per-turn latency instrumentation for Koe Talk — pure, no clock of its own.

The difference between "voice chatbot" and "conversation" is the gap between
the user's last word and the AI's first audio. This module turns that gap into
a number the owner can bench-drive (D14: no quality change without a number):
`talk.py --debug` prints one line per turn, and a session summary at exit.

All timestamps are passed in explicitly (`time.time()` at the call site), so
the math is unit-testable with fake times.
"""

from __future__ import annotations


class TurnTimeline:
    """Named timestamps for one reply epoch. First stamp per name wins, so
    "first sentence" / "first audio" are naturally first-occurrence."""

    # Stage names, in causal order:
    #   user_stopped   last voiced mic block before the turn committed
    #   committed      TurnEngine emitted Commit (end-of-turn wait elapsed)
    #   llm_sentence   first complete sentence out of the LLM stream
    #   first_audio    first reply audio actually playing (or text shown)
    #   done           reply fully generated and fully played
    def __init__(self, epoch: int = 0):
        self.epoch = epoch
        self.stamps: dict[str, float] = {}

    def stamp(self, name: str, t: float) -> None:
        self.stamps.setdefault(name, t)

    def delta(self, a: str, b: str) -> float | None:
        if a in self.stamps and b in self.stamps:
            return self.stamps[b] - self.stamps[a]
        return None

    def gap(self) -> float | None:
        """THE number: user's last voiced moment -> first reply audio."""
        return self.delta("user_stopped", "first_audio")

    def render(self) -> str:
        """One debug line, e.g.:
        gap 1.42s = eot 0.71 + llm 0.50 + tts 0.21
        (eot includes fragment STT — it runs during the end-of-turn wait)"""
        g = self.gap()
        if g is None:
            return "gap n/a"
        parts = []
        for label, a, b in (("eot", "user_stopped", "committed"),
                            ("llm", "committed", "llm_sentence"),
                            ("tts", "llm_sentence", "first_audio")):
            d = self.delta(a, b)
            if d is not None:
                parts.append(f"{label} {d:.2f}")
        return f"gap {g:.2f}s = " + " + ".join(parts) if parts else f"gap {g:.2f}s"


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile (p in 0..100). Tiny and dependency-free —
    enough for a session of a few dozen turns."""
    if not values:
        return 0.0
    xs = sorted(values)
    k = max(0, min(len(xs) - 1, round(p / 100.0 * (len(xs) - 1))))
    return xs[k]


class SessionStats:
    """Aggregates turn gaps for the end-of-session summary (and someday a
    talkbench results log, mirroring bench/results.jsonl)."""

    def __init__(self):
        self.gaps: list[float] = []
        self.barge_ins = 0
        self.turns = 0

    def add(self, timeline: TurnTimeline) -> None:
        self.turns += 1
        g = timeline.gap()
        if g is not None:
            self.gaps.append(g)

    def render(self) -> str:
        if not self.gaps:
            return f"turns={self.turns}  (no completed replies)"
        return (f"turns={self.turns}  gap p50={percentile(self.gaps, 50):.2f}s "
                f"p95={percentile(self.gaps, 95):.2f}s  barge-ins={self.barge_ins}")
