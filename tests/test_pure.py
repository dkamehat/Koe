"""Unit tests for Koe's pure logic — no model, GPU, mic, or network needed.
Run with:  python -m pytest
"""

from koe.formatter import format_text, collapse_runaway_repeats
from koe.dictionary import Dictionary
from koe.refiner import _language_preserved, _num_predict, _find_boundary, _has_cjk
from koe.context_grabber import extract_terms
from koe.translator import already_in_target, language_name


# --- formatter --------------------------------------------------------------

def test_formatter_english_caps_and_newline():
    assert format_text("hello world new line this is a test") == "Hello world\nthis is a test"

def test_formatter_japanese_kaigyou():
    assert format_text("これはテストです 改行 二行目") == "これはテストです\n二行目"

def test_formatter_spoken_period():
    assert format_text("let's go full stop done") == "Let's go. Done"

def test_formatter_disabled_is_passthrough():
    assert format_text("um hello", enable=False) == "um hello"

def test_formatter_keeps_decimal_intact():
    # The '.' in 3.5 must not be treated as a sentence end ("3. 5").
    assert format_text("来週までに3.5%改善します") == "来週までに3.5%改善します"

def test_formatter_keeps_grouped_number_intact():
    assert format_text("予算は1,000万円です") == "予算は1,000万円です"

def test_formatter_still_spaces_real_sentence_break():
    # A period between words should still get a following space.
    assert format_text("done.next") == "Done. Next"

def test_collapse_runaway_repeats_cjk_loop():
    assert collapse_runaway_repeats("私はハビ" + "シャッ" * 100) == "私はハビシャッ"

def test_collapse_runaway_repeats_english_loop():
    assert collapse_runaway_repeats("ok " + "the " * 30 + "end") == "ok the end"

def test_collapse_leaves_normal_text_alone():
    s = "これはテストです。明日の会議は10時からです。"
    assert collapse_runaway_repeats(s) == s


# --- dictionary -------------------------------------------------------------

def test_dictionary_parse_apply_and_prompt(tmp_path):
    p = tmp_path / "dict.txt"
    p.write_text("Foo Bar\nふーばー => Foo Bar\n", encoding="utf-8")
    d = Dictionary(p)
    assert "Foo Bar" in d.terms
    assert d.apply("ふーばーのテスト") == "Foo Barのテスト"
    assert "Foo Bar" in (d.initial_prompt() or "")

def test_dictionary_learn_persists(tmp_path):
    p = tmp_path / "dict.txt"
    d = Dictionary(p)
    d.learn("ほげ", "Hoge")
    assert d.apply("ほげです") == "Hogeです"
    assert "ほげ => Hoge" in p.read_text(encoding="utf-8")

def test_dictionary_longer_rules_win(tmp_path):
    p = tmp_path / "dict.txt"
    p.write_text("えーびー => AB\nえーびーしー => ABC\n", encoding="utf-8")
    d = Dictionary(p)
    assert d.apply("えーびーしー") == "ABC"   # longest match applied first

def test_initial_prompt_is_bare_listing(tmp_path):
    p = tmp_path / "dict.txt"
    p.write_text("ガードレール\nissue\npull request\n", encoding="utf-8")
    d = Dictionary(p)
    assert d.initial_prompt() == "用語: ガードレール、issue、pull request。"


# --- refiner guards ---------------------------------------------------------

def test_language_guard_blocks_translation():
    assert _language_preserved("これは日本語です", "This is English.") is False

def test_language_guard_allows_same_language():
    assert _language_preserved("これは日本語です", "これは日本語です。") is True
    assert _language_preserved("um hello world", "Hello world.") is True

def test_num_predict_bounds():
    assert _num_predict("") == 64
    assert _num_predict("x" * 1000) == 512
    assert 64 <= _num_predict("適度な長さの文章です") <= 512

def test_has_cjk():
    assert _has_cjk("こんにちは") is True
    assert _has_cjk("漢字") is True
    assert _has_cjk("hello world") is False


# --- streaming sentence boundary -------------------------------------------

def test_boundary_japanese():
    assert _find_boundary("これは。残り") == 3

def test_boundary_ascii_needs_whitespace():
    assert _find_boundary("hello. world") == 5

def test_boundary_does_not_split_decimal():
    assert _find_boundary("it is 3.5 times") == -1

def test_boundary_waits_on_trailing_dot():
    assert _find_boundary("the end.") == -1      # wait for next char
    assert _find_boundary("the end. ") == 7      # now it's a boundary


# --- context term extraction ------------------------------------------------

def test_extract_terms_pulls_identifiers():
    terms = extract_terms("def get_user_by_id(user_id): return UserModel")
    assert "get_user_by_id" in terms
    assert "UserModel" in terms


# --- interpreter translation gating -----------------------------------------

def test_already_in_target_skips_same_language():
    # JP text + target ja => already there (no needless LLM call / no self-translate)
    assert already_in_target("ja", "今日は会議です") is True
    assert already_in_target("ja", "this is english") is False
    # EN target: latin-only counts as already-English; CJK does not
    assert already_in_target("en", "David is from the UK") is True
    assert already_in_target("en", "デイビッド") is False

def test_language_name_known_and_passthrough():
    assert language_name("ja") == "Japanese"
    assert language_name("xx") == "xx"   # unknown code passes through unchanged

def test_interpreter_is_question():
    from interpreter import _is_question
    assert _is_question("what motivates you?") is True
    assert _is_question("これでいいですか？") is True
    assert _is_question("the question is what to do") is False

def test_interpreter_to_16k_mono_downmix_and_resample():
    import numpy as np
    from interpreter import _to_16k_mono
    # 0.1 s of 48 kHz stereo: L=1000, R=3000 -> mono 2000; resamples to ~1600 @16k
    frames = 4800
    stereo = np.zeros((frames, 2), dtype=np.int16)
    stereo[:, 0] = 1000
    stereo[:, 1] = 3000
    out = _to_16k_mono(stereo.tobytes(), 48000, 2)
    assert out.dtype == np.float32
    assert abs(len(out) - 1600) <= 1                       # 4800 * 16000/48000
    assert abs(float(out.mean()) - (2000 / 32768)) < 1e-3  # channel-averaged level


def test_leaked_chinese_only_flags_nontarget():
    from koe.translator import leaked_nontarget_chinese
    # 贡 is simplified-Chinese-only (JP uses 貢) -> a leak into a Japanese target
    assert leaked_nontarget_chinese("ja", "私は更多贡献します") is True
    # Correct Japanese (貢献) must NOT be flagged
    assert leaked_nontarget_chinese("ja", "私は貢献します") is False
    # When the target IS Chinese, it's expected, not a leak (future --to zh)
    assert leaked_nontarget_chinese("zh", "我会贡献") is False


# --- reply suggestion prompt (responder) ------------------------------------

def test_responder_prompt_includes_role_and_context():
    from koe.responder import _system_prompt
    p = _system_prompt("English", "PM interview, be concise", "RESUME: built Koe")
    assert "English" in p
    assert "PM interview, be concise" in p
    assert "built Koe" in p          # background material is grounded in the prompt

def test_responder_prompt_minimal_has_no_dangling_sections():
    from koe.responder import _system_prompt
    p = _system_prompt("Japanese", None, None)
    assert "Japanese" in p
    assert "BACKGROUND" not in p     # no empty background block when no context
    assert "context/goal" not in p   # no empty role line when no role
