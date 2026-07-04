# Spec: overlay-v1 — on-screen caption overlay for the Interpreter  (status: implemented)

## Goal

`python interpreter.py --to ja --suggest --overlay` shows a translucent,
click-through, always-on-top strip near the bottom of the screen with the live
caption, its translation, and any suggested reply — so the Interpreter can be
used *over* a call window instead of read from a terminal. Terminal output is
unchanged; the overlay is an additional sink.

## Non-goals

- No interaction (no buttons/drag) — display-only. D17 applies: the overlay
  must never trigger GPU work; it only renders text the pipeline already
  produced.
- No behavior change whatsoever when `--overlay` is absent.
- No new pip dependencies (tkinter is stdlib; it must be imported lazily —
  invariant 2 in CLAUDE.md).

## Context to read (exhaustive)

- CLAUDE.md
- interpreter.py (whole file)
- koe/config.py (config pattern), koe/latency.py (style example for a small
  pure module), tests/test_talk.py (test style)

## Design

### 1. `koe/overlaytext.py` — pure logic (CI-tested, no tkinter import)

```python
KIND_SOURCE, KIND_TRANSLATION, KIND_SUGGESTION = "source", "translation", "suggestion"

def display_secs(text: str) -> float:
    # Reading-time heuristic: 4s floor so short captions don't blink away,
    # +0.15s/char for long ones, 12s cap so stale text can't squat the screen.
    return max(4.0, min(12.0, 4.0 + 0.15 * len(text)))

def wrap_caption(text: str, max_chars: int = 46, max_lines: int = 2) -> list[str]:
    # Line-wrap for display. If the text contains ASCII spaces, wrap on word
    # boundaries; otherwise (Japanese) wrap by character count. If the wrapped
    # text exceeds max_lines, KEEP THE TAIL (newest words carry the meaning in
    # live captions) and prefix the first kept line with "…".

class CaptionItem:            # plain dataclass
    kind: str
    lines: list[str]          # already wrapped
    expires_at: float

class CaptionBuffer:
    def __init__(self, max_items: int = 3): ...
    def add(self, kind: str, text: str, now: float) -> None:
        # wrap_caption + display_secs; append; drop oldest beyond max_items.
        # A new SUGGESTION replaces any existing suggestion (only the latest
        # reply proposal is ever actionable).
    def visible(self, now: float) -> list[CaptionItem]:
        # drop expired items (in place), return the rest oldest-first.
```

All time is passed in (`now`) — no clock inside, same rule as koe/latency.py.

### 2. `koe/overlay.py` — tkinter I/O edge (all tkinter/ctypes inside functions)

```python
def run_overlay(overlay_q, stop, cfg) -> None:
    """Owns the Tk mainloop. MUST run on the process's main thread (tkinter
    is main-thread-only). Drains overlay_q via root.after polling — no other
    thread ever touches a Tk object.
    overlay_q items: (kind, text) tuples. stop: threading.Event — when set by
    the caller, the window closes; when the window dies, run_overlay returns.
    """
```

Implementation requirements:
- Window: `overrideredirect(True)`, `attributes("-topmost", True)`,
  `attributes("-alpha", cfg.overlay_opacity)`. Background color `"#010203"`
  and `wm_attributes("-transparentcolor", "#010203")` so only the text and its
  backing chips are visible.
- Geometry: width = 70% of `winfo_screenwidth()`, anchored bottom-center,
  bottom edge at 90% of screen height.
- Text: one `tk.Label` per visible line, `font=("Yu Gothic UI", cfg.overlay_font_size)`,
  colors — source `white`, translation `#7fdbff`, suggestion `#7cfc00` with a
  `">> "` prefix on its first line. Each label gets a near-black background
  (`#101010`) so text stays readable over any app (the transparent color must
  NOT be used as the label background, or the chip disappears).
- Click-through (best-effort, guarded `try/except` — display still works if it
  fails): after the window is mapped, via ctypes
  `GWL_EXSTYLE = -20; style |= WS_EX_LAYERED(0x00080000) | WS_EX_TRANSPARENT(0x00000020) | WS_EX_NOACTIVATE(0x08000000)`
  using `windll.user32.GetWindowLongPtrW/SetWindowLongPtrW` on
  `root.winfo_id()`'s top-level parent (`windll.user32.GetAncestor(hwnd, 2)`).
- DPI: at start, `ctypes.windll.shcore.SetProcessDpiAwareness(2)` in try/except.
- Poll loop: `root.after(100, pump)`; `pump` drains the queue without blocking
  (`get_nowait` until Empty), feeds `CaptionBuffer.add(..., now=time.time())`,
  re-renders labels from `visible(now)`, checks `stop.is_set()` → `root.destroy()`.
- On any exception during window creation: print one stderr warning
  (`! overlay unavailable (...) — terminal captions only`) and return (D16).

### 3. `interpreter.py` changes (additive; zero behavior change without the flag)

a. Extract the segmentation loop: move the current `try: while True:` body of
   `cmd_run` (block get → RMS → VAD counters → seg cut/preroll trim) into a
   module-level function, *constants and all, verbatim*:

```python
def _segment_loop(raw_q, seg_q, threshold, debug, max_seg_s, stop) -> None:
    # SILENCE_HANG / MIN_SPEECH / MAX_SEG / PREROLL move here with their
    # comments. Loop is `while not stop.is_set():` and the blocking
    # `raw_q.get()` becomes `raw_q.get(timeout=0.5)` + `continue` on
    # queue.Empty so the stop flag is honored within 0.5s.
```

   In `cmd_run`, non-overlay path: `try: _segment_loop(..., stop=stop) except
   KeyboardInterrupt: pass` — identical behavior to today (stop is a fresh
   Event nobody sets). Overlay path: run `_segment_loop` on a daemon thread,
   then `from koe.overlay import run_overlay; run_overlay(overlay_q, stop, cfg)`
   on the main thread; when the mainloop exits (window closed or Ctrl+C),
   `stop.set()` and join the segment thread (timeout 2.0). The existing
   `finally:` cleanup (cap.stop(), sentinel puts, joins) stays and must run in
   both paths.

b. Plumb the sink: `_Transcriber.__init__` and `_SuggestWorker.__init__` gain
   an optional `overlay_q=None` parameter (keyword, last). In
   `_Transcriber.run`: after printing the caption, `overlay_q.put((KIND_SOURCE,
   text))`; for the translation, capture it first (`tr = self._translator.
   translate(text)`) then print and `put((KIND_TRANSLATION, tr))`. In
   `_SuggestWorker.run`: for each rendered line, `put((KIND_SUGGESTION,
   line.strip()))`. All puts guarded by `if self._overlay_q is not None`.

c. CLI: `--overlay` flag in `main()`, passed to `cmd_run(overlay: bool)`.
   `overlay_q` is created only when the flag is set; passing `None` otherwise
   keeps every existing code path byte-identical.

d. Config additions (koe/config.py, comment block, additive only — D18):

```python
# --- Interpreter overlay captions (interpreter.py --overlay) ---
# Translucent, click-through, always-on-top caption strip. Display-only by
# contract (D17): it renders pipeline output, never triggers GPU work.
overlay_opacity: float = 0.85   # window alpha (0.5 barely-there .. 1.0 solid)
overlay_font_size: int = 18     # pt; bump for presentations / small screens
```

## File-by-file changes

| File | Change |
|------|--------|
| koe/overlaytext.py | new, pure (no tkinter/ctypes imports) |
| koe/overlay.py | new, tkinter+ctypes inside functions only |
| interpreter.py | `_segment_loop` extraction; `overlay_q` plumbing; `--overlay` flag; docstring usage line |
| koe/config.py | two config keys with comments |
| tests/test_overlay.py | new, pure tests only |

## Tests to write (tests/test_overlay.py)

- `test_wrap_japanese_by_chars` — 100-char JA string, max 46/2 → 2 lines, tail kept, first line starts with "…"
- `test_wrap_english_on_word_boundaries` — words never split mid-word
- `test_short_text_single_line_untouched`
- `test_display_secs_floor_and_cap` — 4.0 floor, 12.0 cap, monotonic between
- `test_buffer_drops_expired` — item invisible after its expires_at
- `test_buffer_caps_items_oldest_out`
- `test_new_suggestion_replaces_old` — two suggestions → only the newer visible
- `test_overlay_module_imports_without_tkinter_side_effects` — `import koe.overlay` works headless (imports stay lazy)
- `test_interpreter_still_imports` — `import interpreter` after the refactor

## Acceptance criteria

1. All new tests pass; whole suite green with only pytest+requests+numpy.
2. `python -m compileall koe interpreter.py` clean.
3. Without `--overlay`, `interpreter.py`'s observable behavior is unchanged
   (same prints, same thread structure, same Ctrl+C handling) — verified by
   reading the diff: the non-overlay path must call `_segment_loop` with the
   same values previously used inline.
4. `koe/overlaytext.py` imports neither tkinter nor ctypes.
5. Every new constant/config key carries a WHY comment.
6. Self-check table submitted, one row per criterion.

## Manual checks for the owner (Windows)

- Overlay renders over a fullscreen-windowed video call; clicks pass through.
- `--debug` caption latency unchanged (±0.1s) with overlay on vs off.
- Ctrl+C in the terminal closes both the pipeline and the window (known-flaky
  area; if the window lingers, closing it also ends the session — acceptable v1).

## Known pitfalls

- tkinter objects touched from any non-main thread crash — hence the
  after()-polling design; do not "optimize" it into a thread.
- `-transparentcolor` + label background must differ (chips vanish otherwise).
- Some fullscreen-exclusive apps (games) cover topmost windows — document as a
  v1 limitation in the README section (reviewer writes README).
