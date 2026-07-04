"""Unit tests for the Interpreter's on-screen caption overlay (--overlay).

Pure tests only — koe/overlaytext.py has no tkinter/ctypes import, so these
run on any OS with no display (invariant 2). Run with:  python -m pytest
"""

from koe.overlaytext import (CaptionBuffer, KIND_SOURCE, KIND_SUGGESTION,
                             KIND_TRANSLATION, display_secs, wrap_caption)


# --- wrap_caption -------------------------------------------------------------

def test_wrap_japanese_by_chars():
    text = "あ" * 100
    lines = wrap_caption(text, max_chars=46, max_lines=2)
    assert len(lines) == 2
    assert lines[0].startswith("…")

def test_wrap_english_on_word_boundaries():
    text = "the quick brown fox jumps over the lazy dog again and again today"
    lines = wrap_caption(text, max_chars=20, max_lines=2)
    words = set(text.split())
    for line in lines:
        for token in line.replace("…", "").split():
            assert token in words     # no word ever split mid-word

def test_short_text_single_line_untouched():
    assert wrap_caption("hello there") == ["hello there"]
    assert wrap_caption("こんにちは") == ["こんにちは"]

def test_wrap_head_keeps_start_and_marks_end():
    text = "あ" * 100
    lines = wrap_caption(text, max_chars=46, max_lines=2, keep="head")
    assert len(lines) == 2
    assert lines[0] == "あ" * 46              # the start survives, untouched
    assert lines[-1].endswith("…")            # the cut is marked at the end
    assert not lines[0].startswith("…")


# --- display_secs --------------------------------------------------------------

def test_display_secs_floor_and_cap():
    assert display_secs("") == 4.0
    assert display_secs("a" * 200) == 12.0
    short = display_secs("hello")
    long = display_secs("hello " * 10)
    assert 4.0 <= short < long <= 12.0


# --- CaptionBuffer ---------------------------------------------------------------

def test_buffer_drops_expired():
    buf = CaptionBuffer()
    buf.add(KIND_SOURCE, "hi", now=0.0)
    expires_at = buf._items[0].expires_at
    assert any(it.kind == KIND_SOURCE for it in buf.visible(now=expires_at - 0.01))
    assert buf.visible(now=expires_at + 0.01) == []

def test_buffer_caps_items_oldest_out():
    buf = CaptionBuffer(max_items=3)
    buf.add(KIND_SOURCE, "one", now=0.0)
    buf.add(KIND_SOURCE, "two", now=0.0)
    buf.add(KIND_SOURCE, "three", now=0.0)
    buf.add(KIND_SOURCE, "four", now=0.0)
    visible = buf.visible(now=0.0)
    assert len(visible) == 3
    assert all("one" not in "".join(it.lines) for it in visible)
    assert any("four" in "".join(it.lines) for it in visible)

def test_new_suggestion_replaces_old():
    buf = CaptionBuffer()
    buf.add(KIND_SUGGESTION, "first reply", now=0.0)
    buf.add(KIND_SUGGESTION, "second reply", now=0.0)
    visible = buf.visible(now=0.0)
    suggestions = [it for it in visible if it.kind == KIND_SUGGESTION]
    assert len(suggestions) == 1
    assert "second reply" in "".join(suggestions[0].lines)

def test_suggestion_single_item_head_wrapped():
    # The worker joins the reply + gloss into ONE item; head-keep means the
    # start of the reply (what the user will say aloud) is always on screen.
    reply = ">> reply [English]: I think we should ship the feature next week"
    gloss = "[Japanese]: 来週その機能をリリースすべきだと思います、準備はできています"
    buf = CaptionBuffer()
    buf.add(KIND_SUGGESTION, f"{reply} {gloss}", now=0.0)
    visible = buf.visible(now=0.0)
    suggestions = [it for it in visible if it.kind == KIND_SUGGESTION]
    assert len(suggestions) == 1                       # one item, not one per line
    assert ">> reply" in suggestions[0].lines[0]       # reply start on the first line
    assert len(suggestions[0].lines) <= 3

def test_revision_changes_only_on_mutation():
    buf = CaptionBuffer()
    r0 = buf.revision
    buf.add(KIND_SOURCE, "hi", now=0.0)
    assert buf.revision == r0 + 1                      # add bumps
    r1 = buf.revision
    buf.visible(now=0.0)
    assert buf.revision == r1                          # nothing expired: unchanged
    buf.visible(now=1000.0)
    assert buf.revision == r1 + 1                      # expiry drop bumps


# --- module import smoke (CI-level guard for invariant 2: pure core, I/O edges) ---

def test_overlay_module_imports_without_tkinter_side_effects():
    import koe.overlay  # noqa: F401  (tkinter/ctypes must stay lazy, inside run_overlay)

def test_interpreter_still_imports():
    import interpreter  # noqa: F401  (post-refactor sanity: no import-time regressions)
