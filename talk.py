#!/usr/bin/env python
"""Koe Talk (声トーク) — a genuinely sequential, fully local voice conversation.

Speak into the mic, pause, and a local LLM answers OUT LOUD within a couple of
seconds — then you answer back. No hotkey per turn, no cloud, nothing leaves
the machine: faster-whisper (STT) + Ollama (LLM) + VOICEVOX/SAPI (TTS).

This is the third pillar of Koe (dictation → interpreter → talk) and the first
step of the north star in docs/VISION.md: 「AIとのやり取りを本当に逐次的な会話で
実現する」.

Usage:
    python talk.py                       # hands-free conversation (mic + speakers)
    python talk.py --text                # type instead of speaking (no mic needed —
                                         #   works on any OS; great for testing)
    python talk.py --role "英会話の練習相手。優しく訂正して"   # persona
    python talk.py --context brief.md    # ground replies in a briefing file
    python talk.py --ollama-model qwen2.5:14b   # stronger model for the reply
    python talk.py --voice-backend sapi  # force a TTS backend (voicevox/sapi/text)
    python talk.py --speaker 3           # VOICEVOX style id (GET /speakers lists them)
    python talk.py --echo-mode headphones  # enable voice barge-in (see below)
    python talk.py --patience 1.5        # wait longer at pauses before replying
    python talk.py --barge-key f7        # different interrupt hotkey (default f8)
    python talk.py --threshold 0.01      # pin the mic VAD level by hand
    python talk.py --no-calibrate        # skip startup mic calibration
    python talk.py --calibrate-secs 2    # listen longer for the noise floor
    python talk.py --debug               # per-turn latency timeline + RMS meter
    python talk.py --list-devices        # list microphones (--device <index>)

How a turn ends (the whole point): silence alone doesn't end your turn — what
you *said* does. A trailing 「…ですか？」 commits in ~0.5 s; a trailing 「…けど」
or "and" holds your floor for ~2 s so you can think. While the AI is still
thinking (nothing spoken yet), just start talking: the pending reply is
cancelled and your words extend the same turn (「あ、それと…」 works). See
koe/turntaking.py — the state machine is the spec and is fully unit-tested.

Interrupting the AI:
- Any mode: press the barge key (default F8; F9 belongs to the interpreter).
- --echo-mode mute (DEFAULT): the mic is ignored while the AI's voice is
  playing. Structurally echo-proof — safe on open speakers.
- --echo-mode headphones: the mic stays live while the AI speaks and ~0.3 s of
  sustained voice interrupts it mid-word. Use with headphones/earbuds — on open
  speakers the AI may hear itself and self-interrupt.

Spoken commands (whole utterance only): 「貼って」 pastes the AI's last reply
into the focused app (the conversation becomes an input method — koe/injector),
「終了」/"goodbye" ends the session.

Pipeline (single event mailbox; the TurnEngine is the only decision maker):
    mic thread ─► ("audio", block) ─┐
    stt worker ─► ("frag_text", t) ─┤
    llm thread ─► ("sentence"/…)  ──┼─► main loop ─► TurnEngine ─► actions
    player     ─► ("play_start"/…) ─┤      (segments fragments, dispatches
    hotkey     ─► ("barge",)       ─┘       LLM/TTS work, tracks latency)

Requires a mic + `ollama serve` running. TTS degrades VOICEVOX → SAPI → text
(the conversation never dies, it just gets quieter). Stop with Ctrl+C.
"""

from __future__ import annotations

import queue
import sys
import threading
import time

import numpy as np
import requests

from interpreter import _block_rms, _is_hallucination, calibrate_threshold
from koe.config import Config
from koe.latency import SessionStats, TurnTimeline
from koe.refiner import _find_boundary
from koe.turntaking import (Cancel, Commit, ConversationHistory, TurnEngine,
                            bound_reply_tokens, build_system_prompt,
                            parse_talk_command, sanitize_for_speech)
from koe.voice import build_voice

SR = 16000
BLOCK_S = 0.1
PREWARM_EPOCH = -1   # tts_q sentinel: synthesize-and-discard (model warm-up)

# Fragment segmentation (in ~0.1 s blocks). Fragments are cut SHORT — 0.4 s of
# quiet, versus the interpreter's 0.6 s — because turn-level patience lives in
# the TurnEngine's semantic waits, and cutting early means the text (and its
# completeness cue) is known sooner. STT of a fragment runs *during* the
# end-of-turn wait, which is what hides its latency.
FRAG_HANG = 4        # 0.4 s of quiet cuts a fragment
MIN_SPEECH = 2       # >=0.2 s of voice before a fragment is worth transcribing
FRAG_MAX_S = 12.0    # hard cap; monologue turns are legitimate in conversation
PREROLL = 3          # keep 0.3 s of pre-speech so onsets aren't clipped

# ANSI (same palette as koe/app.py).
DIM, BOLD, GREEN, CYAN, YELLOW, RED, RESET = ("\033[2m", "\033[1m", "\033[32m",
                                              "\033[36m", "\033[33m", "\033[31m",
                                              "\033[0m")


class _MicCapture(threading.Thread):
    """Producer: 0.1 s mic blocks (float32 mono @16 kHz) into the mailbox."""

    def __init__(self, device: int | None, mail: "queue.Queue"):
        super().__init__(daemon=True)
        self._device = device
        self._mail = mail
        self._running = True

    def run(self) -> None:
        import sounddevice as sd

        def cb(indata, frames, time_info, status):  # noqa: ARG001
            if self._running:
                self._mail.put(("audio", indata.copy().reshape(-1)))

        try:
            with sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                                device=self._device, blocksize=int(SR * BLOCK_S),
                                callback=cb):
                while self._running:
                    time.sleep(0.2)
        except Exception as exc:
            # Without a mic the session is dead — tell the main loop so it
            # exits instead of sitting silently on an empty mailbox.
            self._mail.put(("mic_dead", str(exc)))

    def stop(self) -> None:
        self._running = False


def _measure_mic_noise(device: int | None, secs: float) -> list[float]:
    """~`secs` of mic RMS levels for calibrate_threshold (mirrors the
    interpreter's loopback measurement, but for the microphone)."""
    import sounddevice as sd
    rms: list[float] = []

    def cb(indata, frames, time_info, status):  # noqa: ARG001
        rms.append(_block_rms(indata.copy().reshape(-1)))

    with sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                        device=device, blocksize=int(SR * BLOCK_S), callback=cb):
        time.sleep(max(0.3, secs))
    return rms


class _SttWorker(threading.Thread):
    """Transcribes cut fragments off the audio path. Single thread => fragment
    texts arrive in spoken order (same rule as the interpreter's transcriber)."""

    def __init__(self, engine, dictionary, frag_q: "queue.Queue", mail: "queue.Queue"):
        super().__init__(daemon=True)
        self._engine = engine
        self._dict = dictionary
        self._frag_q = frag_q
        self._mail = mail

    def run(self) -> None:
        prompt = self._dict.initial_prompt() if self._dict else None
        while True:
            item = self._frag_q.get()
            if item is None:
                break
            gen, audio = item
            text = ""
            if len(audio) / SR >= 0.3:
                try:
                    # Dictionary bias + corrections apply to conversation too —
                    # project jargon must survive the round trip (D04).
                    text = self._engine.transcribe(audio, initial_prompt=prompt).strip()
                    if self._dict:
                        text = self._dict.apply(text)
                    if _is_hallucination(text, audio):
                        text = ""
                except Exception:
                    text = ""
            self._mail.put(("frag_text", gen, text))


def _stream_reply(url: str, model: str, messages: list[dict], epoch: int,
                  mail: "queue.Queue", cancel: threading.Event,
                  num_predict: int) -> None:
    """Stream one Ollama reply, emitting complete sentences into the mailbox.
    Runs on its own short-lived thread with its own Session (streams from
    successive epochs can overlap around a cancel; sharing a Session across
    those threads isn't safe, and connection reuse is noise next to LLM time)."""
    import json

    s = requests.Session()
    s.trust_env = False
    try:
        resp = s.post(
            f"{url.rstrip('/')}/api/chat",
            json={
                "model": model,
                "stream": True,
                # 0.6: conversational variety. The refiner uses 0.2 (mechanical
                # copy-editing) and the responder 0.4; a conversation partner
                # that always says the same thing feels canned.
                "options": {"temperature": 0.6, "num_predict": num_predict},
                "keep_alive": "10m",
                "messages": messages,
            },
            timeout=120,
            stream=True,
        )
        resp.raise_for_status()
        buf = ""
        for line in resp.iter_lines():
            if cancel.is_set():
                break
            if not line:
                continue
            chunk = json.loads(line).get("message", {}).get("content", "")
            if not chunk:
                continue
            buf += chunk
            i = _find_boundary(buf)
            while i != -1:
                sentence, buf = buf[: i + 1], buf[i + 1:]
                if sentence.strip():
                    mail.put(("sentence", epoch, sentence.strip()))
                i = _find_boundary(buf)
        if not cancel.is_set() and buf.strip():
            mail.put(("sentence", epoch, buf.strip()))
    except Exception:
        pass  # llm_done with zero sentences → main loop reports it
    finally:
        mail.put(("llm_done", epoch))


class _TtsWorker(threading.Thread):
    """Synthesizes sentences off the playback path so sentence N+1 renders
    while N is playing. Single thread — SAPI/COM is thread-confined (voice.py)."""

    def __init__(self, voice, tts_q: "queue.Queue", play_q: "queue.Queue",
                 mail: "queue.Queue", turns: TurnEngine):
        super().__init__(daemon=True)
        self._voice = voice
        self._tts_q = tts_q
        self._play_q = play_q
        self._mail = mail
        self._turns = turns

    def run(self) -> None:
        warned = False
        while True:
            item = self._tts_q.get()
            if item is None:
                break
            epoch, sentence = item
            if epoch == PREWARM_EPOCH:
                # Warm the TTS model HERE (not on a helper thread): SAPI/COM
                # and the VOICEVOX session are confined to this thread.
                self._voice.synth(sentence)
                continue
            if epoch != self._turns.epoch:
                continue  # stale: the user interrupted after this was queued
            audio, sr = self._voice.synth(sentence)
            if epoch != self._turns.epoch:
                continue
            if (not audio.size or sr <= 0) and self._voice.name != "text" and not warned:
                # A silent degrade would mislead (D16): say why once.
                warned = True
                print(f"! TTS synthesis failed ({self._voice.last_error or 'no audio'})"
                      f" — showing text only", file=sys.stderr, flush=True)
            # Everything flows through the player queue — even empty audio —
            # so 'spoken' events stay in sentence order when a mid-reply
            # synthesis fails while an earlier sentence is still playing.
            self._play_q.put((epoch, sentence, audio, sr))


class _Player(threading.Thread):
    """Plays synthesized sentences in ~50 ms chunks, checking the current epoch
    between chunks so a barge-in stops audio in <100 ms. The output stream is
    opened once and kept open across sentences (device open costs ~150 ms —
    per-reply latency we don't want to pay twice)."""

    def __init__(self, play_q: "queue.Queue", mail: "queue.Queue", turns: TurnEngine):
        super().__init__(daemon=True)
        self._play_q = play_q
        self._mail = mail
        self._turns = turns

    def run(self) -> None:
        sd = None          # imported on the first playable item (lazy — D15)
        no_audio = False   # once playback is known-broken, degrade to text
        stream = None
        stream_sr = 0
        try:
            while True:
                item = self._play_q.get()
                if item is None:
                    break
                epoch, sentence, audio, sr = item
                if epoch != self._turns.epoch:
                    continue
                if not audio.size or sr <= 0:
                    # Text-only sentence (null backend / failed synth): the
                    # printed text IS the reply. Serialized through this queue
                    # so it can't overtake a sentence that is still sounding.
                    self._mail.put(("spoken", epoch, sentence))
                    continue
                if sd is None and not no_audio:
                    try:
                        import sounddevice as _sd
                        sd = _sd
                    except Exception as exc:
                        no_audio = True
                        print(f"! audio playback unavailable ({exc}) — "
                              f"replies stay text-only", file=sys.stderr, flush=True)
                if no_audio:
                    # The reply must still COMPLETE (else the engine waits in
                    # SPEAKING forever) — the printed text is the reply.
                    self._mail.put(("spoken", epoch, sentence))
                    continue
                try:
                    if stream is None or sr != stream_sr:
                        if stream is not None:
                            stream.close()
                        stream = sd.OutputStream(samplerate=sr, channels=1,
                                                 dtype="float32")
                        stream.start()
                        stream_sr = sr
                    self._mail.put(("play_start", epoch))
                    chunk = max(1, int(sr * 0.05))
                    aborted = False
                    for i in range(0, len(audio), chunk):
                        if epoch != self._turns.epoch:
                            aborted = True
                            break
                        stream.write(np.ascontiguousarray(audio[i:i + chunk]))
                    if not aborted:
                        self._mail.put(("spoken", epoch, sentence))
                except Exception as exc:
                    # A dying output device must not wedge the conversation:
                    # count the sentence as spoken and warn once (D16).
                    print(f"! playback error ({exc}) — continuing text-only",
                          file=sys.stderr, flush=True)
                    no_audio = True
                    stream = None
                    self._mail.put(("spoken", epoch, sentence))
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass


def _prewarm_llm(url: str, model: str) -> None:
    """Load the LLM into VRAM so the first turn isn't slow. Best-effort, on its
    own thread (plain HTTP with its own Session — thread-safe). TTS warm-up is
    NOT done here: synthesis is confined to the TTS worker thread (D22), so the
    caller enqueues a PREWARM_EPOCH item instead."""
    try:
        s = requests.Session()
        s.trust_env = False
        s.post(f"{url.rstrip('/')}/api/chat",
               json={"model": model, "messages": [], "keep_alive": "10m"},
               timeout=60)
    except Exception:
        pass


def _stdin_loop(mail: "queue.Queue") -> None:
    while True:
        try:
            line = input()
        except EOFError:
            mail.put(("line", None))
            return
        mail.put(("line", line))


def cmd_run(text_mode: bool, device: int | None, role: str | None,
            context: str | None, ollama_model: str | None,
            voice_backend: str | None, speaker: int | None,
            echo_mode: str | None, patience: float | None,
            barge_key: str | None, threshold: float | None,
            calibrate: bool, calibrate_secs: float, debug: bool) -> None:
    cfg = Config.load()
    if speaker is not None:
        cfg.voicevox_speaker = speaker
    llm = ollama_model or cfg.talk_model or cfg.ollama_model
    echo = (echo_mode or cfg.talk_echo_mode or "mute").lower()
    key = barge_key or cfg.talk_barge_key
    pat = patience if patience is not None else cfg.talk_patience

    from koe.refiner import _ollama_available
    if not _ollama_available(cfg.ollama_url):
        print(f"! ollama is not running at {cfg.ollama_url} — Koe Talk needs a "
              f"local LLM. Start it with:  ollama serve  (then: ollama pull {llm})",
              file=sys.stderr, flush=True)
        if not text_mode:
            raise SystemExit(1)
    else:
        # A configured-but-not-pulled model would leave the whole conversation
        # silently dead — warn loudly and fall back, like interpreter.py does.
        from interpreter import _installed_models
        installed = _installed_models(cfg.ollama_url)
        if installed and llm not in installed:
            if cfg.ollama_model in installed:
                print(f"! ollama model {llm!r} not installed — using "
                      f"{cfg.ollama_model!r} instead.  Get it with:  ollama pull {llm}",
                      file=sys.stderr, flush=True)
                llm = cfg.ollama_model
            else:
                print(f"! ollama model {llm!r} not installed.  ollama pull {llm}",
                      file=sys.stderr, flush=True)

    voice = build_voice(cfg, voice_backend)
    turns = TurnEngine(patience=pat, barge_by_voice=(echo == "headphones"))
    history = ConversationHistory()
    sysprompt = build_system_prompt(role, context)
    stats = SessionStats()

    mail: "queue.Queue" = queue.Queue()
    frag_q: "queue.Queue" = queue.Queue()
    tts_q: "queue.Queue" = queue.Queue()
    play_q: "queue.Queue" = queue.Queue()

    threading.Thread(target=_prewarm_llm, args=(cfg.ollama_url, llm),
                     daemon=True).start()

    stt = None
    cap = None
    if not text_mode:
        if threshold is None:
            if calibrate:
                print(f"{DIM}calibrating mic noise floor (~{calibrate_secs:g}s; "
                      f"stay quiet) ...{RESET}", flush=True)
                try:
                    samples = _measure_mic_noise(device, calibrate_secs)
                except Exception as exc:
                    raise SystemExit(
                        f"! could not open the microphone: {exc}\n"
                        f"  check --device (see --list-devices)") from exc
                threshold = calibrate_threshold(samples)
            else:
                threshold = 0.005
        print(f"{DIM}loading {cfg.model} ...{RESET}", flush=True)
        from koe.dictionary import Dictionary
        from koe.engine import TranscriptionEngine
        engine = TranscriptionEngine(model=cfg.model, device=cfg.device,
                                     compute_type=cfg.compute_type,
                                     language=cfg.language)
        dictionary = Dictionary() if cfg.enable_dictionary else None
        stt = _SttWorker(engine, dictionary, frag_q, mail)
        stt.start()
        cap = _MicCapture(device, mail)
        cap.start()
    else:
        threading.Thread(target=_stdin_loop, args=(mail,), daemon=True).start()

    tts = _TtsWorker(voice, tts_q, play_q, mail, turns)
    tts.start()
    player = _Player(play_q, mail, turns)
    player.start()
    if voice.name != "text":
        tts_q.put((PREWARM_EPOCH, "はい"))   # warm the TTS model on ITS thread

    hotkey_on = False
    if not text_mode:
        try:
            import keyboard
            keyboard.add_hotkey(key, lambda: mail.put(("barge",)))
            hotkey_on = True
        except Exception as exc:
            print(f"! could not register barge hotkey {key!r}: {exc}",
                  file=sys.stderr, flush=True)

    print(f"\n{BOLD}Koe Talk{RESET} — {'text mode (type below)' if text_mode else 'listening'}."
          f"\n  • LLM → {BOLD}{llm}{RESET} [local]   voice → {BOLD}{voice.name}{RESET}"
          + ("" if text_mode else
             f"\n  • echo mode → {BOLD}{echo}{RESET}"
             + (" (mic ignored while the AI speaks; use --echo-mode headphones for voice barge-in)"
                if echo == "mute" else " (speak over the AI to interrupt it)")
             + f"\n  • interrupt key → {BOLD}{key}{RESET}"
             + (f"   VAD threshold → {threshold:.4f}" if threshold else ""))
          + f"\n  • say 「終了」/'goodbye' to end · 「貼って」 pastes the last reply"
          + f"\n  • Ctrl+C to stop\n", flush=True)

    # --- main loop state ---
    seg: list[np.ndarray] = []
    speech = silence = 0
    in_speech = False
    max_seg_blocks = int(FRAG_MAX_S / BLOCK_S)
    last_voice_t = time.time()
    last_dbg = 0.0
    mic_hold_until = 0.0   # mute mode: keep the mic closed briefly after a reply
    timelines: dict[int, TurnTimeline] = {}
    reply_state: dict[int, dict] = {}   # epoch -> {done, enq, spoken}
    cancels: dict[int, threading.Event] = {}
    last_reply_text = ""

    def reset_seg():
        nonlocal seg, speech, silence, in_speech
        seg, speech, silence, in_speech = [], 0, 0, False

    def finish_reply(epoch: int) -> None:
        nonlocal mic_hold_until
        turns.on_reply_done(epoch)
        tl = timelines.get(epoch)
        if tl:
            tl.stamp("done", time.time())
            stats.add(tl)
            if debug:
                print(f"{DIM}  [{tl.render()}]{RESET}", flush=True)
        if (echo == "mute" and voice.name != "text"
                and tl and "first_audio" in tl.stamps):
            # OutputStream.write returns when the audio is BUFFERED, not when
            # it has finished sounding — hold the mic shut a moment longer so
            # the reply's tail can't re-enter as a phantom user turn.
            mic_hold_until = time.time() + 0.6
        # Retire per-epoch bookkeeping so a long session doesn't accumulate it.
        for d in (timelines, reply_state, cancels):
            for e in [e for e in d if e <= epoch]:
                d.pop(e, None)
        if not in_speech:
            reset_seg()   # drop any stale pre-reply tail
        print(f"{DIM}(listening…){RESET}", flush=True)

    def check_reply_complete(epoch: int) -> None:
        st = reply_state.get(epoch)
        if st and st["done"] and st["spoken"] >= st["enq"]:
            if st["enq"] == 0:
                print(f"{YELLOW}(no reply — is ollama running / model pulled? "
                      f"ollama pull {llm}){RESET}", flush=True)
            finish_reply(epoch)

    def handle(act) -> bool:
        """Dispatch a TurnEngine action. Returns False to quit."""
        nonlocal last_reply_text
        if act is None:
            return True
        if isinstance(act, Commit):
            cmd = parse_talk_command(act.text)
            if cmd == "quit":
                return False
            if cmd == "paste":
                if last_reply_text:
                    try:
                        from koe.injector import inject
                        inject(last_reply_text, cfg.output_mode)
                        print(f"{GREEN}✓ pasted the last reply{RESET}", flush=True)
                    except Exception as exc:
                        print(f"{YELLOW}paste failed: {exc}{RESET}", flush=True)
                else:
                    print(f"{DIM}(nothing to paste yet){RESET}", flush=True)
                turns.on_reply_done(act.epoch)
                return True
            now = time.time()
            print(f"{CYAN}you:{RESET} {act.text}", flush=True)
            history.user(act.text)
            tl = TurnTimeline(act.epoch)
            tl.stamp("user_stopped", last_voice_t)
            tl.stamp("committed", now)
            timelines[act.epoch] = tl
            reply_state[act.epoch] = {"done": False, "enq": 0, "spoken": 0}
            ev = threading.Event()
            cancels[act.epoch] = ev
            msgs = history.messages(sysprompt)
            threading.Thread(target=_stream_reply,
                             args=(cfg.ollama_url, llm, msgs, act.epoch, mail,
                                   ev, bound_reply_tokens(act.text)),
                             daemon=True).start()
            return True
        if isinstance(act, Cancel):
            stats.barge_ins += 1
            for e, ev in cancels.items():
                if e < act.epoch:
                    ev.set()
            if act.merged:
                history.drop_pending_user()
                print(f"{DIM}(reply cancelled — go on){RESET}", flush=True)
            else:
                history.interrupted()
                print(f"{DIM}(interrupted){RESET}", flush=True)
                if echo == "mute":
                    reset_seg()   # blocks buffered before the reply are stale now
            return True
        return True

    try:
        while True:
            try:
                # A finite timeout (instead of a bare blocking get) keeps
                # Ctrl+C deliverable on Windows even if all producers die.
                kind, *payload = mail.get(timeout=0.5)
            except queue.Empty:
                continue

            if kind == "audio":
                block = payload[0]
                rms = _block_rms(block)
                voiced = rms > (threshold or 0.005)
                if voiced:
                    last_voice_t = time.time()
                if debug and time.time() - last_dbg > 0.5:
                    bar = "#" * min(30, int(rms * 400))
                    print(f"  {DIM}rms={rms:.4f} {'VOICED' if voiced else 'quiet '} "
                          f"|{bar:<30}| {turns.state}{RESET}",
                          file=sys.stderr, flush=True)
                    last_dbg = time.time()
                # Mute mode: the mic is dead while the AI's voice is playing —
                # structurally echo-proof (D23) — and stays shut a beat after
                # the reply so the buffered audio tail can't re-enter.
                if echo == "mute" and (turns.state == turns.SPEAKING
                                       or time.time() < mic_hold_until):
                    continue
                if not handle(turns.on_block(voiced)):
                    break
                # Fragment segmentation (the interpreter's loop, shorter hang).
                seg.append(block)
                if voiced:
                    speech += 1
                    silence = 0
                    in_speech = True
                else:
                    silence += 1
                ended = in_speech and silence >= FRAG_HANG and speech >= MIN_SPEECH
                capped = len(seg) >= max_seg_blocks and speech >= MIN_SPEECH
                if ended or capped:
                    gen = turns.on_fragment_cut()
                    frag_q.put((gen, np.concatenate(seg).astype(np.float32)))
                    reset_seg()
                elif in_speech and silence >= FRAG_HANG:
                    # A voiced blip shorter than MIN_SPEECH (a click, a cough
                    # tail) expired: discard it, or the buffer would grow until
                    # the next real utterance becomes one giant fragment.
                    seg = seg[-PREROLL:]
                    speech = silence = 0
                    in_speech = False
                elif not in_speech and len(seg) > PREROLL:
                    seg = seg[-PREROLL:]

            elif kind == "frag_text":
                gen, text = payload
                if not handle(turns.on_fragment_text(text, gen)):
                    break

            elif kind == "mic_dead":
                print(f"{RED}! mic capture failed: {payload[0]} — check --device "
                      f"(see --list-devices){RESET}", file=sys.stderr, flush=True)
                break

            elif kind == "line":
                line = payload[0]
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue
                if turns.state != turns.LISTENING:
                    handle(turns.on_barge_key())
                # Typed input has no mic timing — the moment of Enter is the
                # honest "user stopped" stamp for the latency gap.
                last_voice_t = time.time()
                if not handle(turns.force_commit(line)):
                    break

            elif kind == "sentence":
                epoch, s = payload
                if epoch != turns.epoch:
                    continue
                s = sanitize_for_speech(s)
                if not s:
                    continue
                tl = timelines.get(epoch)
                if tl:
                    tl.stamp("llm_sentence", time.time())
                st = reply_state.get(epoch)
                if st:
                    if st["enq"] == 0:
                        print(f"{GREEN}AI:{RESET} {s}", flush=True)
                    else:
                        print(f"    {s}", flush=True)
                    st["enq"] += 1
                tts_q.put((epoch, s))

            elif kind == "llm_done":
                epoch = payload[0]
                if epoch != turns.epoch:      # cancelled reply — don't complete it
                    reply_state.pop(epoch, None)
                    continue
                st = reply_state.get(epoch)
                if st:
                    st["done"] = True
                    check_reply_complete(epoch)

            elif kind == "play_start":
                epoch = payload[0]
                if epoch == turns.epoch:
                    tl = timelines.get(epoch)
                    if tl:
                        tl.stamp("first_audio", time.time())
                    turns.on_reply_started(epoch)

            elif kind == "spoken":
                epoch, s = payload
                if epoch != turns.epoch:
                    continue
                # For text-only replies there is no play_start: the printed
                # sentence IS the reply reaching the user, so the merge window
                # (THINKING) must close here too.
                turns.on_reply_started(epoch)
                tl = timelines.get(epoch)
                if tl:
                    tl.stamp("first_audio", time.time())  # text-only replies
                history.assistant_spoken(s)
                last_reply_text = (last_reply_text + " " + s).strip() \
                    if reply_state.get(epoch, {}).get("spoken") else s
                st = reply_state.get(epoch)
                if st:
                    st["spoken"] += 1
                    check_reply_complete(epoch)

            elif kind == "barge":
                handle(turns.on_barge_key())

    except KeyboardInterrupt:
        pass
    finally:
        print(f"\n{DIM}{stats.render()}{RESET}")
        if cap is not None:
            cap.stop()
        if stt is not None:
            frag_q.put(None)
        tts_q.put(None)
        play_q.put(None)
        for worker in (stt, tts, player):
            if worker is not None:
                worker.join(timeout=2.0)
        if hotkey_on:
            try:
                import keyboard
                keyboard.remove_hotkey(key)
            except Exception:
                pass
        print(f"{DIM}Bye.{RESET}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]

    if "--list-devices" in argv:
        from koe.recorder import Recorder
        print(Recorder.list_devices())
        return

    def _val(name: str, default=None):
        return (argv[argv.index(name) + 1]
                if name in argv and argv.index(name) + 1 < len(argv) else default)

    context = None
    cpath = _val("--context")
    if cpath:
        from pathlib import Path
        try:
            raw = Path(cpath).read_text(encoding="utf-8")
            context = raw[:8000]
            note = " (truncated to 8000 chars)" if len(raw) > 8000 else ""
            print(f"loaded context: {cpath} ({len(context)} chars){note}", flush=True)
        except Exception as exc:
            print(f"! could not read --context {cpath!r}: {exc}",
                  file=sys.stderr, flush=True)

    thr_raw = _val("--threshold")
    dev_raw = _val("--device")
    spk_raw = _val("--speaker")
    pat_raw = _val("--patience")
    cmd_run(
        text_mode="--text" in argv,
        device=int(dev_raw) if dev_raw is not None else None,
        role=_val("--role"),
        context=context,
        ollama_model=_val("--ollama-model"),
        voice_backend=_val("--voice-backend"),
        speaker=int(spk_raw) if spk_raw is not None else None,
        echo_mode=_val("--echo-mode"),
        patience=float(pat_raw) if pat_raw is not None else None,
        barge_key=_val("--barge-key"),
        threshold=float(thr_raw) if thr_raw is not None else None,
        calibrate="--no-calibrate" not in argv,
        calibrate_secs=float(_val("--calibrate-secs", "1.0")),
        debug="--debug" in argv,
    )


if __name__ == "__main__":
    main()
