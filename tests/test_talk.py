"""Executable spec for Koe Talk's pure core — no mic, GPU, LLM, or TTS needed.

These tests ARE the turn-taking specification: each one pins a conversational
behavior (documented in docs/VISION.md) so a successor can extend the engine
from tests alone. Run with:  python -m pytest
"""

from koe.latency import SessionStats, TurnTimeline, percentile
from koe.turntaking import (COMPLETE, INCOMPLETE, NEUTRAL, QUESTION,
                            WAIT_COMPLETE_MS, WAIT_INCOMPLETE_MS,
                            WAIT_QUESTION_MS, Cancel, Commit,
                            ConversationHistory, INTERRUPTED_MARK, TurnEngine,
                            bound_reply_tokens, build_system_prompt,
                            classify_completeness, parse_talk_command,
                            sanitize_for_speech, wait_ms)
from koe.voice import (is_unwanted_fallback, pick_voice_backend,
                       wav_bytes_to_float32)


# --- semantic endpointing: what a trailing cue means -------------------------

def test_question_is_detected():
    assert classify_completeness("これでいいですか？") == QUESTION
    assert classify_completeness("what do you think?") == QUESTION
    # Japanese questions often arrive without ？ from Whisper.
    assert classify_completeness("どう思いますか") == QUESTION

def test_trailing_te_kedo_hold_the_floor():
    assert classify_completeness("昨日、会議があって") == INCOMPLETE
    assert classify_completeness("行きたいんだけど") == INCOMPLETE
    assert classify_completeness("これは") == INCOMPLETE

def test_whisper_period_does_not_override_kedo():
    # Whisper punctuates aggressively; 「〜ですが。」 is still mid-thought.
    assert classify_completeness("そうなんですが。") == INCOMPLETE

def test_te_form_request_with_period_is_complete():
    # Two-tier rule: て is only a weak cue — 「説明して。」 is a finished
    # request, while unpunctuated 「〜があって」 is a breath pause.
    assert classify_completeness("説明して。") == COMPLETE
    assert classify_completeness("昨日会議があって") == INCOMPLETE

def test_sentence_final_forms_are_complete():
    assert classify_completeness("わかりました。") == COMPLETE
    assert classify_completeness("そう思います") == COMPLETE
    assert classify_completeness("It works fine.") == COMPLETE
    assert classify_completeness("そうですね") == COMPLETE

def test_english_trailing_conjunction_holds():
    assert classify_completeness("we could ship it but") == INCOMPLETE
    assert classify_completeness("first we deploy,") == INCOMPLETE
    assert classify_completeness("I want to") == INCOMPLETE

def test_no_cue_is_neutral():
    assert classify_completeness("はい") == NEUTRAL
    assert classify_completeness("") == NEUTRAL

def test_asymmetry_prefers_holding():
    # False-INCOMPLETE costs ~1s of patience; false-COMPLETE interrupts the
    # user mid-thought. 「ちょっと」 alone is ambiguous — we must hold.
    assert classify_completeness("ちょっと") == INCOMPLETE

def test_ka_fillers_are_not_questions():
    # Bare 「か」 endings are mostly fillers mid-thought — they must NOT take
    # the fastest (question) commit lane; real questions end ですか/ますか/？.
    assert classify_completeness("なんか") == INCOMPLETE
    assert classify_completeness("というか") == INCOMPLETE
    assert classify_completeness("どうですか") == QUESTION

def test_fullwidth_comma_holds_like_ascii():
    assert classify_completeness("そうですね、") == INCOMPLETE

def test_wait_ordering_and_patience():
    assert (wait_ms(QUESTION) < wait_ms(COMPLETE)
            < wait_ms(NEUTRAL) < wait_ms(INCOMPLETE))
    assert wait_ms(COMPLETE, patience=2.0) == 2 * WAIT_COMPLETE_MS
    assert wait_ms(COMPLETE, patience=0.0) == 0.25 * WAIT_COMPLETE_MS  # floored


# --- TurnEngine scenarios -----------------------------------------------------

import math


def quiet(eng, n):
    """Drive n quiet blocks; return the first action emitted (if any)."""
    for _ in range(n):
        act = eng.on_block(False)
        if act:
            return act
    return None


def blocks_for(ms, eng):
    """Quiet blocks needed to reach `ms` of silence."""
    return math.ceil(ms / eng.block_ms)


def say(eng, text, voiced_blocks=3):
    """One utterance fragment: voice, cut, transcription arrives."""
    for _ in range(voiced_blocks):
        eng.on_block(True)
    eng.on_fragment_cut()
    return eng.on_fragment_text(text)


def test_complete_turn_commits_after_short_wait():
    eng = TurnEngine()
    assert say(eng, "わかりました。") is None
    blocks_needed = blocks_for(WAIT_COMPLETE_MS, eng)
    assert quiet(eng, blocks_needed - 1) is None          # not yet
    act = quiet(eng, 1)
    assert isinstance(act, Commit) and act.text == "わかりました。"
    assert eng.state == eng.THINKING and act.epoch == 1

def test_question_commits_faster_than_statement():
    eng = TurnEngine()
    say(eng, "これでいいですか？")
    act = quiet(eng, blocks_for(WAIT_QUESTION_MS, eng))
    assert isinstance(act, Commit)

def test_trailing_kedo_extends_the_hold():
    eng = TurnEngine()
    say(eng, "昨日、会議があったんだけど")
    assert quiet(eng, 12) is None                          # 1.2s: still holding
    act = quiet(eng, blocks_for(WAIT_INCOMPLETE_MS, eng))
    assert isinstance(act, Commit)                          # hard cap: reply anyway

def test_resume_within_hold_merges_into_one_turn():
    eng = TurnEngine()
    say(eng, "昨日、会議があって")
    assert quiet(eng, 10) is None            # pausing to think...
    say(eng, "結論が出なかった。")           # ...resumes: same turn continues
    act = quiet(eng, 10)
    assert isinstance(act, Commit)
    assert act.text == "昨日、会議があって 結論が出なかった。"

def test_no_commit_while_fragment_stt_is_pending():
    eng = TurnEngine()
    eng.on_block(True)
    eng.on_fragment_cut()                    # cut, but no text yet
    assert quiet(eng, 30) is None            # 3s of silence: still waiting on STT
    act = eng.on_fragment_text("わかりました。")
    assert isinstance(act, Commit)           # wait already elapsed → immediate

def test_silence_alone_never_commits():
    eng = TurnEngine()
    assert quiet(eng, 50) is None

def test_speech_during_thinking_cancels_and_merges():
    eng = TurnEngine()
    say(eng, "今日の予定を教えて。")
    c1 = quiet(eng, 10)
    assert isinstance(c1, Commit)
    act = eng.on_block(True)                 # user resumes before any audio
    assert isinstance(act, Cancel) and act.merged
    assert act.epoch == c1.epoch + 1 and eng.state == eng.LISTENING
    say(eng, "あと明日の分もまとめて。")
    c2 = quiet(eng, 25)
    assert isinstance(c2, Commit)
    assert c2.text == "今日の予定を教えて。 あと明日の分もまとめて。"
    assert c2.epoch == c1.epoch + 2

def test_barge_key_interrupts_speech():
    eng = TurnEngine()
    say(eng, "説明して。")
    c = quiet(eng, 10)
    eng.on_reply_started(c.epoch)
    assert eng.state == eng.SPEAKING
    act = eng.on_barge_key()
    assert isinstance(act, Cancel) and not act.merged
    assert act.epoch == c.epoch + 1 and eng.state == eng.LISTENING

def test_barge_key_in_listening_is_ignored():
    eng = TurnEngine()
    assert eng.on_barge_key() is None

def test_voice_barge_needs_sustained_voice():
    eng = TurnEngine(barge_by_voice=True, barge_blocks=3)
    say(eng, "説明して。")
    c = quiet(eng, 10)
    eng.on_reply_started(c.epoch)
    assert eng.on_block(True) is None        # 1 block: could be a cough/echo blip
    assert eng.on_block(True) is None        # 2 blocks
    assert eng.on_block(False) is None       # gap resets the run
    assert eng.on_block(True) is None
    assert eng.on_block(True) is None
    act = eng.on_block(True)                 # 3 consecutive → real interruption
    assert isinstance(act, Cancel)

def test_voice_during_speech_ignored_without_barge_mode():
    eng = TurnEngine(barge_by_voice=False)
    say(eng, "説明して。")
    c = quiet(eng, 10)
    eng.on_reply_started(c.epoch)
    for _ in range(20):
        assert eng.on_block(True) is None    # half-duplex: hotkey only

def test_stale_epoch_events_are_ignored():
    eng = TurnEngine()
    say(eng, "説明して。")
    c = quiet(eng, 10)
    eng.on_barge_key()                       # epoch moves on
    eng.on_reply_started(c.epoch)            # stale
    assert eng.state == eng.LISTENING
    eng.on_reply_done(c.epoch)               # stale
    assert eng.state == eng.LISTENING

def test_full_turn_cycle_returns_to_listening():
    eng = TurnEngine()
    say(eng, "こんにちは。")
    c = quiet(eng, 10)
    eng.on_reply_started(c.epoch)
    eng.on_reply_done(c.epoch)
    assert eng.state == eng.LISTENING
    c2 = None
    say(eng, "次の質問です。")
    c2 = quiet(eng, 10)
    assert isinstance(c2, Commit) and c2.epoch == c.epoch + 1

def test_force_commit_for_text_mode():
    eng = TurnEngine()
    act = eng.force_commit("typed input")
    assert isinstance(act, Commit) and act.text == "typed input"
    assert eng.state == eng.THINKING

def test_typed_line_during_thinking_merges_not_replaces():
    # --text mode: typing while the reply is still generating must EXTEND the
    # turn (the history dropped the pending user message on that promise).
    eng = TurnEngine()
    eng.force_commit("私の名前は田中です")
    cancel = eng.on_barge_key()              # typed mid-THINKING → merge-cancel
    assert isinstance(cancel, Cancel) and cancel.merged
    act = eng.force_commit("覚えた？")
    assert act.text == "私の名前は田中です 覚えた？"

def test_inflight_fragment_after_reset_is_dropped():
    # Headphones mode: the user backchannels 「うん」 over the AI's reply. The
    # fragment's STT is still in flight when the reply finishes; its stale
    # generation must be dropped — the AI must NOT answer a backchannel.
    eng = TurnEngine(barge_by_voice=True)
    say(eng, "説明して。")
    c = quiet(eng, 10)
    eng.on_reply_started(c.epoch)
    gen = eng.on_fragment_cut()              # 「うん」 cut while AI speaks
    eng.on_reply_done(c.epoch)               # reply ends; turn state resets
    assert eng.on_fragment_text("うん", gen) is None
    assert quiet(eng, 30) is None            # no phantom commit

def test_voice_barge_starts_a_fresh_turn():
    # merge=False means "the next utterance is NEW": backchannel text picked
    # up while the AI talked must not prefix the interrupting turn.
    eng = TurnEngine(barge_by_voice=True)
    say(eng, "長く説明して。")
    c = quiet(eng, 10)
    eng.on_reply_started(c.epoch)
    eng.on_fragment_cut()
    eng.on_fragment_text("うん")             # backchannel landed pre-barge
    for _ in range(3):
        act = eng.on_block(True)             # sustained voice → barge
    assert isinstance(act, Cancel) and not act.merged
    say(eng, "違う、そうじゃなくて。")
    c2 = quiet(eng, 25)
    assert c2.text == "違う、そうじゃなくて。"   # no 「うん」 prefix


# --- conversation history ------------------------------------------------------

def test_history_records_only_spoken_sentences():
    h = ConversationHistory()
    h.user("説明して。")
    h.assistant_spoken("まず前提から。")
    h.assistant_spoken("次に本題です。")
    h.user("わかった。")                      # closes the assistant turn
    msgs = h.messages("SYS")
    assert msgs[0]["role"] == "system"
    assert msgs[2] == {"role": "assistant", "content": "まず前提から。 次に本題です。"}

def test_history_interruption_leaves_a_marker():
    h = ConversationHistory()
    h.user("説明して。")
    h.assistant_spoken("まず前提から。")
    h.interrupted()
    msgs = h.messages("SYS")
    assert msgs[-1]["content"].endswith(INTERRUPTED_MARK)

def test_history_drop_pending_user_undoes_merge_source():
    h = ConversationHistory()
    h.user("最初の発言")
    h.drop_pending_user()
    assert h.messages("SYS") == [{"role": "system", "content": "SYS"}]

def test_language_pin_rides_the_last_user_message():
    h = ConversationHistory()
    h.user("こんにちは")
    assert "Reply in Japanese" in h.messages("SYS")[-1]["content"]
    h2 = ConversationHistory()
    h2.user("hello there")
    assert "Reply in English" in h2.messages("SYS")[-1]["content"]

def test_history_trims_oldest_first():
    h = ConversationHistory(max_chars=40)
    h.user("a" * 30)
    h.assistant_spoken("b" * 30)
    h.user("c" * 30)
    h.assistant_spoken("d" * 10)
    h.user("最後")
    msgs = h.messages("SYS")
    contents = [m["content"] for m in msgs[1:]]
    assert "a" * 30 not in contents          # oldest dropped
    assert any("最後" in c for c in contents)  # newest kept

def test_system_prompt_grounding_blocks():
    p = build_system_prompt("面接官として厳しめに", "RESUME: built Koe")
    assert "面接官として厳しめに" in p
    assert "built Koe" in p
    p2 = build_system_prompt(None, None)
    assert "BACKGROUND" not in p2


# --- spoken commands -------------------------------------------------------------

def test_commands_match_whole_utterance():
    assert parse_talk_command("終了") == "quit"
    assert parse_talk_command("終了。") == "quit"
    assert parse_talk_command("Goodbye!") == "quit"
    assert parse_talk_command("貼って") == "paste"
    assert parse_talk_command("Paste it.") == "paste"

def test_command_near_misses_do_not_fire():
    assert parse_talk_command("貼ってほしいと言われた") is None
    assert parse_talk_command("終了しないでください") is None
    assert parse_talk_command("the paste it button") is None
    assert parse_talk_command("会議は終了しました") is None


# --- TTS hygiene -------------------------------------------------------------------

def test_sanitize_strips_markdown_and_emoji():
    raw = "**大事な点**は3つ。\n- まず一つ\n`code` です 🎉"
    out = sanitize_for_speech(raw)
    assert "*" not in out and "-" not in out.split()[0] and "`" not in out
    assert "🎉" not in out
    assert "大事な点は3つ" in out.replace(" ", "")

def test_sanitize_drops_code_fences():
    assert "print" not in sanitize_for_speech("見て。```\nprint(1)\n``` 以上。")

def test_sanitize_keeps_meaningful_unit_symbols():
    # The So-category sweep is for emoji — 25℃ must not become 25.
    assert "℃" in sanitize_for_speech("今日は25℃です 🎉")
    assert "🎉" not in sanitize_for_speech("今日は25℃です 🎉")

def test_sanitize_flattens_newlines():
    assert "\n" not in sanitize_for_speech("一行目。\n\n二行目。")

def test_reply_token_bound_is_flat_and_positive():
    assert bound_reply_tokens("短い") == bound_reply_tokens("長い" * 200) > 0


# --- latency instrumentation ---------------------------------------------------------

def test_timeline_gap_and_render():
    tl = TurnTimeline(1)
    tl.stamp("user_stopped", 10.0)
    tl.stamp("committed", 10.7)
    tl.stamp("llm_sentence", 11.2)
    tl.stamp("first_audio", 11.5)
    assert abs(tl.gap() - 1.5) < 1e-9
    assert "gap 1.50s" in tl.render() and "eot 0.70" in tl.render()

def test_timeline_first_stamp_wins():
    tl = TurnTimeline(1)
    tl.stamp("first_audio", 5.0)
    tl.stamp("first_audio", 9.0)             # later sentences don't move it
    assert tl.stamps["first_audio"] == 5.0

def test_percentiles_and_stats():
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([1, 2, 3, 4, 5], 95) == 5
    assert percentile([], 50) == 0.0
    st = SessionStats()
    tl = TurnTimeline(1)
    tl.stamp("user_stopped", 0.0)
    tl.stamp("first_audio", 2.0)
    st.add(tl)
    assert "p50=2.00s" in st.render()


# --- TTS backend selection & WAV decoding ----------------------------------------------

def test_voice_fallback_chain():
    assert pick_voice_backend("auto", True, True) == "voicevox"
    assert pick_voice_backend("auto", False, True) == "sapi"
    assert pick_voice_backend("auto", False, False) == "text"
    # An explicit but unavailable backend degrades LOUDLY to text — a silent
    # stand-in would fake the experience (same rule as bench's refiner warning).
    assert pick_voice_backend("voicevox", False, True) == "text"
    assert pick_voice_backend("sapi", False, False) == "text"
    assert pick_voice_backend("none", True, True) == "text"

def test_fallback_warning_only_when_request_not_honored():
    assert is_unwanted_fallback("voicevox", "text") is True
    assert is_unwanted_fallback("voicevox", "voicevox") is False
    assert is_unwanted_fallback("auto", "text") is False       # any rung is fine
    assert is_unwanted_fallback("none", "text") is False       # text IS the ask
    assert is_unwanted_fallback("NONE", "text") is False       # case-insensitive

def test_wav_roundtrip_downmix():
    import io
    import wave
    import numpy as np
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(24000)
    stereo = np.zeros((240, 2), dtype=np.int16)
    stereo[:, 0] = 1000
    stereo[:, 1] = 3000
    w.writeframes(stereo.tobytes())
    w.close()
    audio, sr = wav_bytes_to_float32(buf.getvalue())
    assert sr == 24000 and len(audio) == 240
    assert abs(float(audio.mean()) - (2000 / 32768)) < 1e-3

def test_wav_garbage_is_silent_not_fatal():
    audio, sr = wav_bytes_to_float32(b"not a wav at all")
    assert len(audio) == 0 and sr == 0


# --- module import smoke (CI-level guard for invariant 2: pure core, I/O edges) ---

def test_talk_module_imports_without_windows_deps():
    import talk  # noqa: F401  (sounddevice/keyboard must stay lazy)
