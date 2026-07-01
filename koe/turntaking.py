"""Turn-taking core for Koe Talk (声トーク) — the pure, executable spec.

This module is the conversation's brain: WHO has the floor, WHEN a pause means
"your turn" versus "I'm still thinking", and HOW an in-flight reply is cancelled
when the user speaks again. It contains **zero I/O** — no audio, no HTTP, no
threads — so every turn-taking behavior is unit-tested on CI, and any live bug
can be reproduced by replaying its event sequence through `TurnEngine` offline.
`talk.py` owns the microphone/LLM/TTS edges and feeds events in.

Design (see docs/VISION.md and docs/DECISIONS.md D22–D27):

- **Semantic endpointing.** A fixed silence timeout is what makes voice chatbots
  feel like walkie-talkies: it interrupts slow thinkers and hesitates after
  clear questions. Instead, end-of-turn waits depend on what was said:
  a turn ending in 「…ですか？」 commits fast; one ending in 「…けど」 or "and"
  holds the user's floor much longer. The classifier is deliberately
  **asymmetric** (D24): calling a finished turn INCOMPLETE costs ~1 s of
  patience; calling an unfinished turn COMPLETE interrupts the user mid-thought
  and costs trust. When unsure, hold.
- **Epoch cancellation.** Every committed turn gets a new epoch; every
  downstream artifact (LLM stream, TTS synthesis, playback chunk) carries its
  epoch and is dropped when stale. Interruption = bump the epoch — there is no
  thread-killing, no flag soup, no race (D25).
- **Merge on resume.** If the user speaks again while the reply is still being
  generated (nothing spoken yet), the pending turn is cancelled and the already
  -committed text is *kept*, so 「あ、それと…」 naturally extends the same turn.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


# --- end-of-turn waits (milliseconds of silence before the turn commits) -----
#
# The silence clock starts at the last voiced block, so fragment STT time
# (~0.3–0.6 s on GPU) is *hidden inside* these waits: by the time the last
# fragment's text arrives, most of the wait has usually already elapsed and a
# COMPLETE turn commits almost immediately. Values are scaled by the single
# user knob `talk_patience` (1.0 = these defaults).
WAIT_QUESTION_MS = 450     # a question yields the floor explicitly — answer fast
WAIT_COMPLETE_MS = 650     # sentence-final form (です/ます/。/.) — normal hand-off
WAIT_NEUTRAL_MS = 1000     # no clear cue — mild patience
WAIT_INCOMPLETE_MS = 2000  # trailing けど/て/and/, — the user is mid-thought; hold
                           # their floor. This is also the hard cap: even a lost
                           # train of thought gets a reply ~2 s in.

QUESTION, COMPLETE, NEUTRAL, INCOMPLETE = "question", "complete", "neutral", "incomplete"

_WAITS = {
    QUESTION: WAIT_QUESTION_MS,
    COMPLETE: WAIT_COMPLETE_MS,
    NEUTRAL: WAIT_NEUTRAL_MS,
    INCOMPLETE: WAIT_INCOMPLETE_MS,
}

# Japanese sentence-final question markers (checked after stripping 。).
# Deliberately NO bare 「か」: fillers (なんか/というか/確か) end in か and would
# take the FASTEST commit lane exactly when the user is mid-thought. Real
# questions overwhelmingly end in these longer forms (or carry ？).
_JA_QUESTION_ENDS = ("ですか", "ますか", "でしょうか", "ましたか", "ませんか",
                     "のか", "かな", "かい")
# Japanese conjunctive/continuative endings — high-precision "definitely not
# finished" cues (D24: this list must stay conservative; a missing entry costs
# ~1 s of patience, a wrong entry interrupts people). Two tiers:
# STRONG cues are contrastive/causal conjunctions and fillers — mid-thought
# even when Whisper appended a 。 (「そうなんですが。」 is not finished).
_JA_INCOMPLETE_STRONG = (
    "けれども", "けれど", "ですが", "ますが", "なので", "だから", "それで",
    "えっと", "ええと", "あのー", "あの", "とか", "たり", "って",
    "というか", "っていうか", "なんか", "確か",
    "けど", "から", "ので", "のに", "そして", "また",
    "が", "し", "と",
)
# WEAK cues are case particles / the te-form — unfinished ONLY when Whisper
# produced no sentence-final punctuation: 「説明して。」 is a complete request,
# 「昨日会議があって」 is a pause for breath.
_JA_INCOMPLETE_WEAK = ("で", "て", "に", "は", "を", "も", "や")
# Japanese sentence-final forms that signal a completed clause.
_JA_COMPLETE_ENDS = (
    "です", "ます", "ました", "でした", "ません", "ですね", "ますね",
    "だよ", "だね", "ですよ", "ますよ", "た", "ない", "ね", "よ", "な", "わ",
)
# English trailing words that mean the sentence isn't done.
_EN_INCOMPLETE_WORDS = {
    "and", "but", "so", "or", "because", "then", "like", "um", "uh",
    "the", "a", "an", "to", "with", "that", "if", "when", "which",
}


def classify_completeness(text: str) -> str:
    """Classify a (possibly partial) turn's trailing cue: QUESTION / COMPLETE /
    INCOMPLETE / NEUTRAL. Pure string heuristics — Whisper's own punctuation is
    a strong signal, but trailing particles override a stray 。 (Whisper
    punctuates aggressively; 「〜ですが。」 is still mid-thought).
    """
    t = text.strip()
    if not t:
        return NEUTRAL
    if t.endswith(("？", "?")):
        return QUESTION
    punctuated = t.endswith(("。", "．", ".", "!", "！"))
    # Look at the last clause, with trailing punctuation peeled off.
    core = t.rstrip("。．.!！、,…・ 　")
    if not core:
        return NEUTRAL
    for end in _JA_QUESTION_ENDS:
        if core.endswith(end):
            return QUESTION
    incomplete_ends = _JA_INCOMPLETE_STRONG + (() if punctuated else _JA_INCOMPLETE_WEAK)
    for end in incomplete_ends:
        if core.endswith(end):
            # ...but a longer complete ending that *contains* the particle wins
            # (「〜でした」 must not match a bare た-adjacent cue).
            if any(core.endswith(c) and len(c) > len(end) for c in _JA_COMPLETE_ENDS):
                break
            return INCOMPLETE
    last_word = core.split()[-1].lower() if core.split() else ""
    if last_word in _EN_INCOMPLETE_WORDS or t.endswith((",", "、")):
        return INCOMPLETE
    for end in _JA_COMPLETE_ENDS:
        if core.endswith(end):
            return COMPLETE
    if punctuated:
        return COMPLETE
    return NEUTRAL


def wait_ms(completeness: str, patience: float = 1.0) -> float:
    """Silence (ms) required to commit a turn with this trailing cue."""
    return _WAITS.get(completeness, WAIT_NEUTRAL_MS) * max(0.25, patience)


# --- engine actions -----------------------------------------------------------

@dataclass
class Commit:
    """Send this user turn to the LLM. Everything downstream carries `epoch`."""
    text: str
    epoch: int


@dataclass
class Cancel:
    """Stop the in-flight reply (LLM stream + TTS + playback). `epoch` is the
    NEW current epoch — anything tagged with an older epoch is stale."""
    epoch: int
    merged: bool = False   # True when the cancelled turn's text was kept for merging


class TurnEngine:
    """The turn-taking state machine. Single-threaded by contract: `talk.py`
    funnels all events through one mailbox and calls these methods from one
    thread. Time is block-based (`block_ms` per `on_block` call) so tests drive
    it deterministically with no clock.

    States:
      LISTENING — the user has (or may take) the floor; fragments accumulate.
      THINKING  — a turn was committed; the LLM is generating; nothing has been
                  spoken yet, so user speech cancels the reply and merges.
      SPEAKING  — the reply is being played. Interruption is a hotkey (any echo
                  mode) or, with `barge_by_voice`, a sustained run of voiced
                  blocks (headphones mode; see D23).
    """

    LISTENING, THINKING, SPEAKING = "listening", "thinking", "speaking"

    def __init__(self, patience: float = 1.0, barge_by_voice: bool = False,
                 barge_blocks: int = 3, block_ms: float = 100.0):
        self.patience = patience
        self.barge_by_voice = barge_by_voice
        # 3 blocks = 300 ms of sustained voice: long enough to reject coughs
        # and echo transients, short enough that yielding still feels instant.
        self.barge_blocks = barge_blocks
        self.block_ms = block_ms
        self.state = self.LISTENING
        self.epoch = 0
        # Turn generation: bumped whenever the building turn is discarded
        # (commit, reply-done reset, non-merge cancel). A fragment whose STT
        # was in flight across a reset arrives with a stale generation and is
        # dropped — speech captured during a reply can never seed the next
        # turn as a phantom (the epoch's counterpart for the *input* side).
        self.gen = 0
        self._texts: list[str] = []      # fragment texts of the turn being built
        self._pending_frags = 0          # fragments cut but not yet transcribed
        self._silence_blocks = 0         # blocks since the last voiced block
        self._heard_voice = False        # any voice since the turn started
        self._barge_run = 0              # consecutive voiced blocks while SPEAKING

    # --- event: one ~100 ms audio block was classified voiced/quiet ---------
    def on_block(self, voiced: bool) -> Commit | Cancel | None:
        if self.state == self.LISTENING:
            if voiced:
                self._silence_blocks = 0
                self._heard_voice = True
            else:
                self._silence_blocks += 1
            return self._maybe_commit()
        if self.state == self.THINKING:
            # Nothing is playing yet, so this can only be the user (or a cough
            # — cheap either way): cancel the pending reply and keep the text
            # so the resumed speech extends the same turn.
            if voiced:
                return self._cancel(merge=True)
            return None
        if self.state == self.SPEAKING:
            if not self.barge_by_voice:
                return None
            self._barge_run = self._barge_run + 1 if voiced else 0
            if self._barge_run >= self.barge_blocks:
                return self._cancel(merge=False)
            return None
        return None

    # --- event: the segmenter cut a fragment (its STT result will follow) ---
    def on_fragment_cut(self) -> int:
        """Returns the current generation; the caller must hand it back with
        the transcription so a fragment that crossed a turn reset is dropped."""
        self._pending_frags += 1
        return self.gen

    # --- event: a fragment's transcription arrived ("" = discarded) ---------
    def on_fragment_text(self, text: str, gen: int | None = None) -> Commit | None:
        if gen is not None and gen != self.gen:
            return None   # cut before a reset (reply-done / barge): stale, drop
        self._pending_frags = max(0, self._pending_frags - 1)
        text = text.strip()
        if text:
            self._texts.append(text)
        return self._maybe_commit()

    # --- event: first reply audio actually started playing ------------------
    def on_reply_started(self, epoch: int) -> None:
        if epoch == self.epoch and self.state == self.THINKING:
            self.state = self.SPEAKING
            self._barge_run = 0

    # --- event: reply fully generated AND fully played -----------------------
    def on_reply_done(self, epoch: int) -> None:
        if epoch == self.epoch and self.state in (self.THINKING, self.SPEAKING):
            self.state = self.LISTENING
            self._reset_turn()

    # --- event: the user pressed the interrupt hotkey ------------------------
    def on_barge_key(self) -> Cancel | None:
        if self.state in (self.THINKING, self.SPEAKING):
            # Keep the text only when nothing was spoken yet (THINKING) — same
            # rationale as voice-during-THINKING; after speech started, the
            # user heard (part of) an answer, so the next utterance is new.
            return self._cancel(merge=self.state == self.THINKING)
        return None

    # --- direct commit for --text mode (typed input has no VAD) -------------
    def force_commit(self, text: str) -> Commit:
        """APPENDS to the building turn rather than replacing it: after a
        merge-cancel (typing while the reply was still generating) the
        previous line must survive into the merged commit — that is the
        promise `drop_pending_user()` relies on (D28)."""
        self._texts.append(text)
        self._pending_frags = 0
        return self._commit()

    # --- internals -----------------------------------------------------------
    def _maybe_commit(self) -> Commit | None:
        if self.state != self.LISTENING or self._pending_frags > 0 or not self._texts:
            return None
        text = " ".join(self._texts)
        needed = wait_ms(classify_completeness(text), self.patience)
        if self._silence_blocks * self.block_ms >= needed:
            return self._commit()
        return None

    def _commit(self) -> Commit:
        text = " ".join(self._texts).strip()
        self.epoch += 1
        self.state = self.THINKING
        self._last_commit_texts = list(self._texts)
        self._reset_turn()
        return Commit(text, self.epoch)

    def _cancel(self, merge: bool) -> Cancel:
        self.epoch += 1          # everything tagged with the old epoch is now stale
        self.state = self.LISTENING
        if merge:
            # The committed text returns to the building turn; new fragments
            # append (same generation — in-flight STT belongs to this turn).
            self._texts = list(getattr(self, "_last_commit_texts", [])) + self._texts
            self._silence_blocks = 0
            self._barge_run = 0
        else:
            # A barge after speech started: the next utterance is NEW. Clear
            # any backchannel text picked up while the AI was talking, and
            # bump the generation so its in-flight STT is dropped too.
            self._reset_turn()
        return Cancel(self.epoch, merged=merge)

    def _reset_turn(self) -> None:
        self.gen += 1            # in-flight fragment STT from before is now stale
        self._texts = []
        self._pending_frags = 0
        self._silence_blocks = 0
        self._heard_voice = False
        self._barge_run = 0


# --- conversation history (what the LLM sees) ----------------------------------

INTERRUPTED_MARK = "（途中で遮られた）"

# Spoken-reply style. Kept strict for the same reason as the refiner's prompt:
# a 7B model follows examples of *shape* better than prose rules (D21). The
# short-first-sentence rule exists for latency: _find_boundary cuts sentence 1
# early, so TTS starts while the rest is still generating.
TALK_SYSTEM_PROMPT = (
    "You are a voice conversation partner. Your words are spoken aloud by TTS, "
    "so answer the way a person talks:\n"
    "- Reply in the SAME language the user spoke (Japanese in -> Japanese out).\n"
    "- 1 to 3 SHORT sentences. Make the FIRST sentence very short (a few words).\n"
    "- Plain speech only: no markdown, no lists, no headings, no emoji, no code.\n"
    "- Be concrete and direct; it's fine to ask one short question back.\n"
    "- If the previous assistant message ends with "
    f"{INTERRUPTED_MARK}, you were cut off mid-reply: do not repeat it, "
    "just respond to what the user says next."
)


def build_system_prompt(role: str | None, context: str | None) -> str:
    """TALK_SYSTEM_PROMPT + optional persona and briefing material, in the same
    shape as koe/responder.py (role sentence, fenced BACKGROUND block)."""
    base = TALK_SYSTEM_PROMPT
    if role:
        base += f"\nThe user's context/goal for this conversation: {role}."
    if context:
        base += (
            "\n\nThe user prepared background material. Ground your replies in it "
            "and do NOT invent details beyond it:\n"
            "----- BACKGROUND -----\n" + context + "\n----- END -----"
        )
    return base


class ConversationHistory:
    """Rolling chat history that records only what was actually SPOKEN.

    The LLM may generate five sentences, but if the user barged in after two,
    the model must believe it said two — that is how a person who was cut off
    behaves. `assistant_spoken()` is fed per played sentence by the player;
    `interrupted()` closes the turn with a marker the system prompt explains.
    """

    def __init__(self, max_chars: int = 6000):
        # ~6000 chars ≈ a long conversation while staying far inside a 7B
        # model's context and keeping prompt-eval latency flat.
        self.max_chars = max_chars
        self._messages: list[dict] = []
        self._current: list[str] = []    # spoken sentences of the open assistant turn

    def user(self, text: str) -> None:
        self._close_assistant()
        self._messages.append({"role": "user", "content": text})
        self._trim()

    def assistant_spoken(self, sentence: str) -> None:
        self._current.append(sentence)

    def interrupted(self) -> None:
        if self._current:
            self._current.append(INTERRUPTED_MARK)
        self._close_assistant()

    def drop_pending_user(self) -> None:
        """Undo the last user message (its reply was cancelled and its text
        merged back into the building turn — it will be re-sent as part of the
        merged commit)."""
        if self._messages and self._messages[-1]["role"] == "user":
            self._messages.pop()

    def _close_assistant(self) -> None:
        if self._current:
            self._messages.append(
                {"role": "assistant", "content": " ".join(self._current).strip()})
            self._current = []
            self._trim()

    def messages(self, system_prompt: str, next_user: str | None = None) -> list[dict]:
        """Full message list for the Ollama chat call. The language pin rides on
        the last user message (prompt-level rules alone don't hold on 7B — D21)."""
        msgs = [{"role": "system", "content": system_prompt}]
        msgs.extend(self._messages)
        if next_user is not None:
            msgs.append({"role": "user", "content": next_user})
        if msgs[-1]["role"] == "user":
            lang = "Japanese" if _has_cjk_local(msgs[-1]["content"]) else "English"
            msgs[-1] = {"role": "user",
                        "content": msgs[-1]["content"]
                        + f"\n\n(Reply in {lang}, as short spoken sentences.)"}
        return msgs

    def _trim(self) -> None:
        # Drop oldest turns first; a conversation's tail matters most.
        while (sum(len(m["content"]) for m in self._messages) > self.max_chars
               and len(self._messages) > 2):
            self._messages.pop(0)


def _has_cjk_local(s: str) -> bool:
    # Same charset logic as koe.refiner._has_cjk, duplicated to keep this
    # module dependency-free (the refiner may grow imports this spec must not).
    return any(
        "぀" <= c <= "ヿ" or "一" <= c <= "鿿" or "ｦ" <= c <= "ﾝ"
        for c in s
    )


# --- spoken commands ------------------------------------------------------------

# Conservative by design: a command fires only when the WHOLE utterance is the
# command (after stripping punctuation/whitespace). 「貼ってほしいと言われた」
# must never paste (tests pin the near-misses).
_QUIT_COMMANDS = {"終了", "会話終了", "会話を終了", "バイバイ", "goodbye", "exit", "quit"}
_PASTE_COMMANDS = {"貼って", "貼り付けて", "ペースト", "ペーストして",
                   "paste", "paste it", "paste that"}

_PUNCT_RE = re.compile(r"[。．\.、,！!？\?…\s]+")


def parse_talk_command(text: str) -> str | None:
    """Return "quit" / "paste" when the utterance IS that command, else None."""
    t = _PUNCT_RE.sub(" ", text).strip().casefold()
    compact = t.replace(" ", "")
    if t in _QUIT_COMMANDS or compact in _QUIT_COMMANDS:
        return "quit"
    if t in _PASTE_COMMANDS or compact in _PASTE_COMMANDS:
        return "paste"
    return None


# --- TTS text hygiene ------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"```.*?(```|$)", re.DOTALL)
_MD_MARKS_RE = re.compile(r"[*_`#>]+")
_BULLET_RE = re.compile(r"^[\s]*[-•・]\s+", re.MULTILINE)
# So-category symbols that CARRY MEANING when spoken — never strip these
# (the So sweep is for emoji; 25℃ must not become 25).
_MEANING_SYMBOLS = set("℃℉°〒※")


def sanitize_for_speech(text: str) -> str:
    """Strip whatever a TTS voice would read out ridiculously: code fences,
    markdown marks, bullets, emoji/symbols. The prompt forbids these, but a 7B
    model slips — this is the deterministic guard behind the rule (same
    philosophy as D02: never trust the prompt alone)."""
    t = _CODE_FENCE_RE.sub(" ", text)
    t = _BULLET_RE.sub("", t)
    t = _MD_MARKS_RE.sub("", t)
    t = "".join(c for c in t
                if unicodedata.category(c) != "So" or c in _MEANING_SYMBOLS)
    return re.sub(r"\s+", " ", t).strip()


def bound_reply_tokens(user_text: str) -> int:
    """Token cap for a spoken reply (D20: every LLM call gets an explicit
    bound). Conversation replies are short by contract — 1-3 sentences — so the
    cap is flat rather than input-scaled, with headroom for CJK tokenization."""
    return 220
