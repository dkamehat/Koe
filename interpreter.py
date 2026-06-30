#!/usr/bin/env python
"""Koe Interpreter — live, on-device captions for *system* audio (WASAPI loopback).

Captures whatever is playing on your speakers (a meeting, a video, a call),
transcribes it locally with the same faster-whisper engine Koe uses for the mic,
and prints rolling captions in the terminal. Nothing leaves the machine.

Sibling of `bench.py` in the Koe series: same engine, different source (the
speaker's WASAPI loopback instead of the mic) and a streaming front-end.

Usage:
    python interpreter.py                 # live captions of the default speaker
    python interpreter.py --list          # list loopback devices you can capture
    python interpreter.py --device Realtek # capture a device whose name contains this
    python interpreter.py --to ja          # translate captions to Japanese (local ollama)
    python interpreter.py --to en          # ...or any target: en/zh/ko/es/fr/de/...
    python interpreter.py --translate      # fast EN-only via Whisper's own translation
    python interpreter.py --threshold 0.01 # pin the VAD level (skips auto-calibration)
    python interpreter.py --no-calibrate   # skip startup calibration, use the static default
    python interpreter.py --calibrate-secs 2  # listen longer when measuring the noise floor
    python interpreter.py --max-seg 6      # shorter hard cap = less lag on long monologues
    python interpreter.py --suggest        # press F9 for a suggested reply (the apex)
    python interpreter.py --auto-suggest   # auto-line up a reply under each question
    python interpreter.py --suggest-key f8 # use a different hotkey
    python interpreter.py --to ja --ollama-model qwen2.5:14b  # stronger LLM for translation
    python interpreter.py --debug          # RMS meter + per-caption (+latency) readout

`--to <lang>` transcribes verbatim, then translates each caption with the local
Ollama server (the same one the dictation refiner uses) and prints source + target.
It needs Ollama running; if it isn't, captions fall back to source-only.

`--suggest` (the apex): in a live foreign-language call, press a hotkey (default
F9) and Koe reads the recent transcript and prints a reply you can say back — in
the call's language + a gloss in your language (--to / default ja). Optional
--role "..." sets a short persona; --context <file> pre-loads briefing material
(your resume, the job description, the meeting agenda) so replies are grounded in
it. Local-only via Ollama.
    python interpreter.py --to ja --suggest --role "PM interview, be concise"
    python interpreter.py --to ja --suggest --context brief.md  # ground replies in a file

By default the VAD voicing threshold is **auto-calibrated** at startup: Koe
listens to the loopback noise floor for ~1 s and sets the threshold just above
it (so you don't have to hand-tune --threshold per machine/source). Pass
--threshold to pin it, or --no-calibrate to use the static default.

Stop with Ctrl+C. Requires `PyAudioWPatch` (WASAPI loopback on Windows).

Pipeline (three decoupled stages so capture never stalls on the GPU):
    capture thread ─► raw blocks ─► segmenter (energy VAD) ─► utterances
                                                              ─► transcribe thread ─► captions
faster-whisper isn't streaming, so audio is cut into utterances at short silence
gaps (or a hard length cap) — clean, non-overlapping captions on natural pauses.
The speaker runs at 48 kHz stereo; we downmix + resample to 16 kHz mono for Whisper.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from collections import deque

import numpy as np

SR = 16000              # Whisper's sample rate (we resample loopback down to this)
BLOCK_S = 0.1           # ~0.1 s per captured block (VAD granularity)

# VAD voicing threshold. Without --threshold, Koe measures the loopback noise
# floor at startup and derives one (see calibrate_threshold); these are the knobs.
DEFAULT_THRESHOLD = 0.005   # static fallback (and the absolute floor for auto)
CALIB_PCTL = 35             # robust floor = this RMS percentile (ignores speech spikes)
CALIB_MARGIN = 2.5          # threshold sits this far above the measured floor
CALIB_FLOOR = 0.005         # never go below this (digital silence -> RMS ~0)
CALIB_CEILING = 0.03        # never go above this; kept below typical speech RMS
                            # (~0.06) so a loud calibration window can't gate out speech

# Whisper's stock outputs on near-silence; drop them when the clip is too short
# to plausibly contain them (avoids phantom "Thank you." captions between pauses).
_HALLUCINATION = {
    "thank you.", "thank you", "thanks for watching.", "thanks for watching",
    "you", "bye.", "ご視聴ありがとうございました", "ご視聴ありがとうございました。",
    "ありがとうございました", "ありがとうございました。", "おやすみなさい。",
}


def _pa():
    import pyaudiowpatch as pyaudio
    return pyaudio


def _default_loopback(p):
    pa = _pa()
    wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
    dout = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
    for d in p.get_loopback_device_info_generator():
        if dout["name"] in d["name"]:
            return d
    for d in p.get_loopback_device_info_generator():   # fallback: any loopback
        return d
    raise SystemExit("no WASAPI loopback device found (need Windows + PyAudioWPatch).")


def _pick_loopback(p, name_substr: str | None):
    if not name_substr:
        return _default_loopback(p)
    for d in p.get_loopback_device_info_generator():
        if name_substr.lower() in d["name"].lower():
            return d
    raise SystemExit(f"no loopback device matches {name_substr!r} — see `interpreter.py --list`")


def cmd_list() -> None:
    pa = _pa()
    p = pa.PyAudio()
    try:
        default = _default_loopback(p)["name"]
        print("Loopback devices available for capture:\n")
        for d in p.get_loopback_device_info_generator():
            mark = "  <- default" if d["name"] == default else ""
            print(f"  {d['name']}{mark}")
        print("\nUse:  python interpreter.py --device \"<part of the name>\"")
    finally:
        p.terminate()


def _to_16k_mono(raw: bytes, in_rate: int, channels: int) -> np.ndarray:
    """int16 interleaved bytes -> float32 mono @ 16 kHz (downmix + linear resample)."""
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        a = a.reshape(-1, channels).mean(axis=1)
    if in_rate != SR and a.size:
        n_out = int(round(a.size * SR / in_rate))
        if n_out <= 0:
            return np.zeros(0, dtype=np.float32)
        xp = np.linspace(0.0, 1.0, num=a.size, endpoint=False)
        xq = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        a = np.interp(xq, xp, a).astype(np.float32)
    return a


def _block_rms(block: np.ndarray) -> float:
    """Root-mean-square level of one audio block (0.0 for an empty block)."""
    return float(np.sqrt(np.mean(block * block))) if block.size else 0.0


def calibrate_threshold(rms_samples, margin: float = CALIB_MARGIN,
                        floor: float = CALIB_FLOOR, ceiling: float = CALIB_CEILING,
                        pctl: float = CALIB_PCTL) -> float:
    """Derive a VAD voicing threshold from measured per-block RMS levels.

    Pure (no I/O) so it's unit-tested. Uses a LOW percentile as the noise floor
    so speech that slips into the calibration window (loopback can't be told to
    go quiet) inflates the floor far less than a mean would, then sits `margin`×
    above it. Clamped to [floor, ceiling]: digital silence (RMS ~0) can't drive
    the threshold to zero, and loud audio mid-calibration can't push it so high
    that real speech is missed.
    """
    arr = np.asarray(list(rms_samples), dtype=np.float32)
    if arr.size == 0:
        return floor
    noise = float(np.percentile(arr, pctl))
    return float(min(ceiling, max(floor, noise * margin)))


def _measure_noise(dev: dict, secs: float) -> list[float]:
    """Capture ~`secs` of loopback and return its per-block RMS levels (the input
    to calibrate_threshold). Opens its own short-lived stream so it runs before
    the capture/transcribe threads start."""
    pa = _pa()
    p = pa.PyAudio()
    rate = int(dev["defaultSampleRate"])
    ch = int(dev["maxInputChannels"])
    chunk = max(1, int(rate * BLOCK_S))
    st = p.open(format=pa.paInt16, channels=ch, rate=rate,
                frames_per_buffer=chunk, input=True,
                input_device_index=dev["index"])
    rms: list[float] = []
    try:
        for _ in range(max(1, int(secs / BLOCK_S))):
            raw = st.read(chunk, exception_on_overflow=False)
            rms.append(_block_rms(_to_16k_mono(raw, rate, ch)))
    finally:
        st.stop_stream()
        st.close()
        p.terminate()
    return rms


class _Capture(threading.Thread):
    """Producer: stream loopback blocks (resampled to 16 kHz mono) into a queue."""

    def __init__(self, dev: dict, q: "queue.Queue[np.ndarray]"):
        super().__init__(daemon=True)
        self._dev = dev
        self._q = q
        self._running = True

    def run(self) -> None:
        pa = _pa()
        p = pa.PyAudio()
        rate = int(self._dev["defaultSampleRate"])
        ch = int(self._dev["maxInputChannels"])
        chunk = max(1, int(rate * BLOCK_S))
        st = p.open(format=pa.paInt16, channels=ch, rate=rate,
                    frames_per_buffer=chunk, input=True,
                    input_device_index=self._dev["index"])
        try:
            while self._running:
                raw = st.read(chunk, exception_on_overflow=False)
                self._q.put(_to_16k_mono(raw, rate, ch))
        finally:
            st.stop_stream()
            st.close()
            p.terminate()

    def stop(self) -> None:
        self._running = False


def _is_hallucination(text: str, audio: np.ndarray) -> bool:
    return text.strip().lower() in _HALLUCINATION and (len(audio) / SR) < 1.6


def _is_question(text: str) -> bool:
    """A natural 'your turn to reply' cue for --auto-suggest. Fires when the
    utterance contains a question mark anywhere (in live speech the '?' often
    isn't the last character of a segment)."""
    return "?" in text or "？" in text


def _installed_models(url: str) -> set:
    from koe.refiner import _ollama_session
    try:
        r = _ollama_session.get(url.rstrip("/") + "/api/tags", timeout=3)
        return {m["name"] for m in r.json().get("models", [])}
    except Exception:
        return set()


class _SuggestHelper:
    """Builds the reply-suggestion lines, shared by the F9 hotkey and the auto path
    so both render identically. Returns lines (caller prints) — no I/O of its own."""

    def __init__(self, suggester, gloss, reply_lang: str | None, gloss_lang: str):
        self.suggester = suggester
        self.gloss = gloss
        self.reply_lang = reply_lang          # None => auto-detect from the transcript
        self.gloss_lang = gloss_lang

    def lines(self, convo: list[str], indent: str = "  ") -> list[str]:
        from koe.refiner import _has_cjk
        from koe.translator import language_name
        if not convo:
            return []
        rl = self.reply_lang or ("ja" if _has_cjk(convo[-1]) else "en")
        reply = self.suggester.suggest(convo, rl)
        if not reply:
            return []
        out = [f"{indent}>> reply [{language_name(rl)}]: {reply}"]
        if rl != self.gloss_lang:
            out.append(f"{indent}   [{language_name(self.gloss_lang)}]: "
                       f"{self.gloss.translate(reply)}")
        return out


class _SuggestWorker(threading.Thread):
    """Generates reply suggestions OFF the transcribe path so captions never stall
    on the LLM, and serializes F9 + auto requests (one ollama call at a time).
    Queue items are (transcript_snapshot, manual) or None to stop."""

    def __init__(self, helper: "_SuggestHelper", q: "queue.Queue"):
        super().__init__(daemon=True)
        self._helper = helper
        self._q = q

    def run(self) -> None:
        while True:
            req = self._q.get()
            if req is None:
                break
            convo, manual = req
            lines = self._helper.lines(convo)
            if lines:
                print("\n" + "\n".join(lines) + "\n", flush=True)
            elif manual:
                print("  (no suggestion)\n", flush=True)


class _Transcriber(threading.Thread):
    """Consumer: pull whole utterances and caption them, off the capture path so
    the GPU never stalls audio. One thread => captions stay in spoken order."""

    def __init__(self, engine, task: str, seg_q: "queue.Queue", t0: float,
                 translator=None, debug: bool = False, transcript: "deque | None" = None,
                 suggest_q: "queue.Queue | None" = None, auto_suggest: bool = False):
        super().__init__(daemon=True)
        self._engine = engine
        self._task = task
        self._q = seg_q
        self._t0 = t0
        self._translator = translator
        self._debug = debug
        self._transcript = transcript
        self._suggest_q = suggest_q
        self._auto = auto_suggest

    def run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            audio, t_voice = item
            if len(audio) / SR < 0.4:        # too short to be real speech
                continue
            text = self._engine.transcribe(audio, task=self._task).strip()
            if not text or _is_hallucination(text, audio):
                continue
            if self._transcript is not None:
                self._transcript.append(text)   # context for --suggest
            stamp = time.strftime("%M:%S", time.gmtime(time.time() - self._t0))
            # Felt latency: the speaker's last voiced moment -> caption on screen.
            suffix = f"  (+{time.time() - t_voice:.1f}s)" if self._debug else ""
            print(f"[{stamp}] {text}{suffix}", flush=True)
            if self._translator is not None:
                # Source printed immediately above; translation follows when ready.
                print(f"          ↳ {self._translator.translate(text)}", flush=True)
            # Auto-suggest: when the other party asks something, hand a reply
            # request to the worker (never block captions). Coalesce bursts: only
            # enqueue when the worker is idle so questions don't pile up.
            if (self._auto and self._suggest_q is not None and _is_question(text)
                    and self._suggest_q.empty()):
                self._suggest_q.put((list(self._transcript), False))


def cmd_run(device: str | None, task: str, threshold: float | None, debug: bool,
            to: str | None, max_seg_s: float, suggest: bool = False,
            suggest_key: str = "f9", reply_lang: str | None = None,
            role: str | None = None, context: str | None = None,
            auto_suggest: bool = False, ollama_model: str | None = None,
            calibrate: bool = True, calibrate_secs: float = 1.0) -> None:
    try:
        import pyaudiowpatch  # noqa: F401
    except ImportError:
        raise SystemExit(
            "PyAudioWPatch is required for loopback capture.\n"
            "  install:  .\\.venv\\Scripts\\python.exe -m pip install PyAudioWPatch"
        )
    pa = _pa()

    from koe.config import Config
    from koe.engine import TranscriptionEngine

    cfg = Config.load()
    # The interpreter's LLM (translation + suggestion) can differ from the dictation
    # refiner's: keep dictation fast on 7b, run the interpreter on a stronger model
    # (e.g. qwen2.5:14b) for cleaner translation. --ollama-model overrides; default
    # = the configured model.
    llm = ollama_model or cfg.ollama_model
    # If the chosen LLM isn't pulled, say so loudly and fall back (else translation
    # silently errors out and echoes the source text).
    if (to or suggest or auto_suggest):
        installed = _installed_models(cfg.ollama_url)
        if installed and llm not in installed:
            if cfg.ollama_model in installed:
                print(f"! ollama model {llm!r} not installed — using {cfg.ollama_model!r} "
                      f"instead.  Get it with:  ollama pull {llm}",
                      file=sys.stderr, flush=True)
                llm = cfg.ollama_model
            else:
                print(f"! ollama model {llm!r} not installed.  ollama pull {llm}",
                      file=sys.stderr, flush=True)
    p = pa.PyAudio()
    try:
        dev = _pick_loopback(p, device)
    finally:
        p.terminate()

    # VAD threshold: an explicit --threshold always wins; otherwise measure the
    # loopback noise floor now (before the capture threads start) and derive one,
    # unless --no-calibrate forces the static default.
    auto_thr = threshold is None and calibrate
    if threshold is None:
        if calibrate:
            print(f"calibrating noise floor from {dev['name']} "
                  f"(~{calibrate_secs:g}s; --threshold/--no-calibrate to skip) ...",
                  flush=True)
            samples = _measure_noise(dev, calibrate_secs)
            threshold = calibrate_threshold(samples)
            floor = np.percentile(samples, CALIB_PCTL) if samples else 0.0
            print(f"  noise floor p{CALIB_PCTL}={floor:.4f} -> threshold={threshold:.4f}",
                  flush=True)
        else:
            threshold = DEFAULT_THRESHOLD

    # Translation target (--to): transcribe verbatim, then translate via local ollama.
    translator = None
    if to:
        from koe.refiner import _ollama_available
        from koe.translator import OllamaTranslator, language_name
        if _ollama_available(cfg.ollama_url):
            translator = OllamaTranslator(llm, cfg.ollama_url, to)
        else:
            print(f"! ollama not running at {cfg.ollama_url} — captions will be "
                  f"source-only (start ollama to translate to {language_name(to)}).",
                  file=sys.stderr, flush=True)

    # Reply suggestion: keep a rolling transcript and build a suggester. Triggered
    # on-demand by the F9 hotkey (--suggest) and/or automatically on questions
    # (--auto-suggest). Needs ollama (same as --to).
    want_suggest = suggest or auto_suggest
    transcript = deque(maxlen=16) if want_suggest else None
    helper = None
    if want_suggest:
        from koe.refiner import _ollama_available
        if _ollama_available(cfg.ollama_url):
            from koe.responder import ReplySuggester
            from koe.translator import OllamaTranslator
            suggester = ReplySuggester(llm, cfg.ollama_url, role, context)
            gloss = OllamaTranslator(llm, cfg.ollama_url, to or "ja")
            helper = _SuggestHelper(suggester, gloss, reply_lang, to or "ja")
        else:
            print("! ollama not running — reply suggestions disabled.",
                  file=sys.stderr, flush=True)
            transcript = None

    print(f"loading {cfg.model} ...", flush=True)
    engine = TranscriptionEngine(model=cfg.model, device=cfg.device,
                                 compute_type=cfg.compute_type, language=cfg.language)
    if translator is not None:
        mode = f"transcribe -> translate to {translator.lang} ({llm})"
    elif task == "translate":
        mode = "translate->EN (Whisper)"
    else:
        mode = "transcribe"
    print(f"\nKoe Interpreter — capturing: {dev['name']}\n"
          f"mode={mode}  threshold={threshold:.4f}{' (auto)' if auto_thr else ''}  "
          f"(Ctrl+C to stop)\n", flush=True)

    # Suggestion worker: all reply generation (F9 + auto) runs here, off the
    # transcribe path and serialized so captions never stall on the LLM.
    suggest_q: "queue.Queue | None" = None
    worker = None
    if helper is not None:
        suggest_q = queue.Queue()
        worker = _SuggestWorker(helper, suggest_q)
        worker.start()

    # Register the F9 hotkey (global, so it works while the call is focused).
    hotkey_on = False
    if helper is not None:
        def _suggest_now():
            # transcript is mutated by the transcribe thread; snapshot defensively
            # (a deque at maxlen can be mutated mid-iteration -> RuntimeError).
            try:
                convo = list(transcript)
            except RuntimeError:
                convo = list(transcript)
            if not convo:
                print("\n  (no speech captured yet)\n", flush=True)
                return
            print("\n  ... thinking of a reply ...", flush=True)
            suggest_q.put((convo, True))

        try:
            import keyboard
            keyboard.add_hotkey(suggest_key, _suggest_now)
            hotkey_on = True
            extra = " (auto on questions)" if auto_suggest else ""
            print(f"(press {suggest_key} any time for a suggested reply{extra})\n",
                  flush=True)
        except Exception as exc:
            print(f"! could not register hotkey {suggest_key!r}: {exc}",
                  file=sys.stderr, flush=True)

    # VAD / segmentation parameters (in ~0.1 s blocks).
    SILENCE_HANG = 6     # 0.6 s of quiet ends an utterance
    MIN_SPEECH = 3       # >=0.3 s of speech before a flush is worthwhile
    MAX_SEG = max(20, int(max_seg_s / BLOCK_S))  # hard cap (--max-seg); lower = less lag
    PREROLL = 3          # keep 0.3 s of pre-speech so onsets aren't clipped

    raw_q: "queue.Queue[np.ndarray]" = queue.Queue()
    seg_q: "queue.Queue" = queue.Queue()
    cap = _Capture(dev, raw_q)
    t0 = time.time()
    tr = _Transcriber(engine, task, seg_q, t0, translator, debug, transcript,
                      suggest_q, auto_suggest)
    cap.start()
    tr.start()

    seg: list[np.ndarray] = []
    speech = silence = 0
    in_speech = False
    last_dbg = 0.0
    last_voice_t = t0     # wall-clock of the most recent voiced block (for latency)
    try:
        while True:
            block = raw_q.get()
            rms = _block_rms(block)
            voiced = rms > threshold
            if debug and time.time() - last_dbg > 0.5:
                bar = "#" * min(40, int(rms * 400))
                print(f"  rms={rms:.4f} {'VOICED' if voiced else 'quiet '} "
                      f"|{bar:<40}| pending={seg_q.qsize()}", file=sys.stderr, flush=True)
                last_dbg = time.time()

            seg.append(block)
            if voiced:
                speech += 1
                silence = 0
                in_speech = True
                last_voice_t = time.time()
            else:
                silence += 1

            ended = in_speech and silence >= SILENCE_HANG and speech >= MIN_SPEECH
            capped = len(seg) >= MAX_SEG and speech >= MIN_SPEECH
            if ended or capped:
                seg_q.put((np.concatenate(seg).astype(np.float32), last_voice_t))
                seg, speech, silence, in_speech = [], 0, 0, False
            elif not in_speech and len(seg) > PREROLL:
                # Discard leading silence so the buffer (and latency) stays small.
                seg = seg[-PREROLL:]
    except KeyboardInterrupt:
        print("\nstopping ...", flush=True)
    finally:
        cap.stop()
        seg_q.put(None)
        tr.join(timeout=2.0)
        if worker is not None:
            suggest_q.put(None)
            worker.join(timeout=2.0)
        if hotkey_on:
            try:
                import keyboard
                keyboard.remove_hotkey(suggest_key)
            except Exception:
                pass


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]
    if "--list" in argv:
        cmd_list()
        return

    def _val(name: str, default=None):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default

    device = _val("--device")
    to = _val("--to")  # target language for local-ollama translation (e.g. "ja")
    # --to (ollama, any language) takes precedence; --translate is Whisper's own EN.
    if to:
        task = "transcribe"
    elif "--translate" in argv:
        task = "translate"
    else:
        task = "transcribe"
    # No --threshold => None, meaning "auto-calibrate from the noise floor at
    # startup" (unless --no-calibrate). An explicit --threshold always wins.
    thr_raw = _val("--threshold")
    threshold = float(thr_raw) if thr_raw is not None else None
    calibrate = "--no-calibrate" not in argv
    calibrate_secs = float(_val("--calibrate-secs", "1.0"))
    max_seg = float(_val("--max-seg", "8"))   # hard segment cap in seconds (latency)
    debug = "--debug" in argv
    suggest = "--suggest" in argv
    auto_suggest = "--auto-suggest" in argv
    suggest_key = _val("--suggest-key", "f9")
    reply_lang = _val("--reply-lang")          # None => auto-detect from the transcript
    role = _val("--role")
    # --context <file>: pre-load briefing material (resume, JD, agenda) to ground replies.
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
            print(f"! could not read --context {cpath!r}: {exc}", file=sys.stderr, flush=True)
    ollama_model = _val("--ollama-model")   # override the LLM for translate/suggest
    cmd_run(device, task, threshold, debug, to, max_seg,
            suggest, suggest_key, reply_lang, role, context, auto_suggest, ollama_model,
            calibrate, calibrate_secs)


if __name__ == "__main__":
    main()
