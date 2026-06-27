"""Koe (声) — main application loop.

Hold the hotkey, speak, release. Audio is transcribed on-device and the cleaned
text is typed into whatever window has focus. Nothing leaves the machine.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import keyboard

from .config import Config
from .dictionary import Dictionary
from .engine import TranscriptionEngine
from .injector import inject
from .recorder import Recorder
from .refiner import build_refiner

# ANSI colors (Windows Terminal / modern consoles support these).
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


class KoeApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.recorder = Recorder(cfg.sample_rate, cfg.input_device)
        self.dictionary = Dictionary() if cfg.enable_dictionary else None
        self.refiner = build_refiner(cfg)
        self.engine: TranscriptionEngine | None = None
        self._recording = False
        self._busy_lock = threading.Lock()
        # Ignore key events we generate ourselves (Ctrl+V paste) so they can't
        # be mistaken for the hotkey.
        self._suppress = False
        # Runtime state for the tray UI.
        self._ready = False           # engine loaded & usable
        self._loading = False         # engine (re)loading in progress
        self._hotkey_handle = None    # current keyboard.hook handle
        self._last_raw = ""           # last transcription before refine (for learning)
        self._last_final = ""         # last text we typed
        self._status_text = "starting…"
        self.on_status_change = None  # optional callback(str) for the tray title

    # --- model loading ---------------------------------------------------
    def _status(self, msg: str) -> None:
        self._status_text = msg
        if callable(self.on_status_change):
            try:
                self.on_status_change(msg)
            except Exception:
                pass

    def load_model(self) -> None:
        self._loading = True
        self._status(f"loading {self.cfg.model}…")
        print(f"{DIM}Loading model '{self.cfg.model}' …{RESET}")
        t0 = time.time()
        engine = TranscriptionEngine(
            model=self.cfg.model,
            device=self.cfg.device,
            compute_type=self.cfg.compute_type,
            language=self.cfg.language,
        )
        with self._busy_lock:
            self.engine = engine
        self._ready = True
        self._loading = False
        dt = time.time() - t0
        dev = f"{engine.device}/{engine.compute_type}"
        self._status("ready")
        print(f"{GREEN}✓ Model ready:{RESET} {BOLD}{self.cfg.model}{RESET} "
              f"{DIM}({dev}, {dt:.1f}s){RESET}")

    def reload_engine(self, model: str) -> None:
        """Swap to a different Whisper model at runtime (heavy; runs in a thread)."""
        if self._loading or model == self.cfg.model:
            return
        self.cfg.model = model
        self.cfg.save()
        self._ready = False
        threading.Thread(target=self.load_model, daemon=True).start()

    def reload_refiner(self, backend: str) -> None:
        """Switch the ③ correction backend at runtime (cheap)."""
        self.cfg.refiner_backend = backend
        self.cfg.save()
        self.refiner = build_refiner(self.cfg)
        self._status("ready")

    def set_ollama_model(self, model: str) -> None:
        """Switch the local refiner model (e.g. qwen2.5:3b fast / 7b quality)."""
        self.cfg.ollama_model = model
        self.cfg.save()
        self.refiner = build_refiner(self.cfg)
        self._status("ready")

    # --- recording lifecycle ---------------------------------------------
    def _start_recording(self) -> None:
        if self._recording:
            return
        if not self._ready:
            print(f"\r{YELLOW}… still loading model, please wait{RESET}     ")
            return
        self._recording = True
        self._status("recording…")
        try:
            self.recorder.start()
            print(f"\r{RED}● REC{RESET}  {DIM}speak…{RESET}        ", end="", flush=True)
        except Exception as exc:
            self._recording = False
            self._status("ready")
            print(f"\r{RED}mic error: {exc}{RESET}")

    def _stop_and_transcribe(self) -> None:
        if not self._recording:
            return
        self._recording = False
        audio = self.recorder.stop()
        dur = audio.size / self.cfg.sample_rate
        if dur < 0.3:
            print(f"\r{YELLOW}… too short, skipped{RESET}            ")
            return
        print(f"\r{CYAN}◌ transcribing{RESET} {DIM}({dur:.1f}s audio)…{RESET}     ",
              end="", flush=True)
        self._status("transcribing…")
        # Transcribe off the hotkey thread so the key handler stays responsive.
        threading.Thread(target=self._transcribe_job, args=(audio,), daemon=True).start()

    def _transcribe_job(self, audio) -> None:
        with self._busy_lock:
            t0 = time.time()
            assert self.engine is not None
            prompt = self.dictionary.initial_prompt() if self.dictionary else None
            terms = list(self.dictionary.terms) if self.dictionary else []
            # #2 Context grounding runs CONCURRENTLY with STT so its cost (UI
            # Automation can be slow on some apps) is hidden under transcription,
            # and is hard-capped by a timeout so it can never stall dictation.
            ctx_box: dict = {}
            ctx_thread = None
            if self.cfg.enable_context:
                def _grab():
                    try:
                        from .context_grabber import get_context
                        ctx_box["ctx"] = get_context(self.cfg.context_max_chars,
                                                     self.cfg.context_include_field)
                    except Exception:
                        ctx_box["ctx"] = None
                ctx_thread = threading.Thread(target=_grab, daemon=True)
                ctx_thread.start()

            raw = self.engine.transcribe(audio, initial_prompt=prompt)
            if self.dictionary:
                raw = self.dictionary.apply(raw)  # fix known mis-transcriptions
            t_stt = time.time() - t0

            context = None
            if ctx_thread is not None:
                ctx_thread.join(timeout=self.cfg.context_timeout)
                context = ctx_box.get("ctx")
                if context:
                    try:
                        from .context_grabber import extract_terms
                        for t in extract_terms(context):
                            if t not in terms:
                                terms.append(t)
                    except Exception:
                        pass
            t_ctx = time.time() - t0 - t_stt
            # ③ Refiner. Stream sentence-by-sentence (local ollama only) so text
            # starts appearing seconds sooner on long dictation; else one-shot.
            self._last_raw = raw
            t_ref0 = time.time()
            if self._can_stream():
                text = self._refine_streaming(raw, terms, context)
            else:
                text = self.refiner.refine(raw, terms, context=context)
                if self.dictionary:
                    text = self.dictionary.apply(text)
                text = self._emit_final(text)
            t_ref = time.time() - t_ref0
            dt = time.time() - t0
            if not text:
                self._status("ready")
                print(f"\r{YELLOW}… (no speech detected){RESET}            ")
                return
            self._status("ready")
            print(f"\r{GREEN}✓{RESET} {text}  "
                  f"{DIM}[{dt:.1f}s = STT {t_stt:.1f} · 文脈 {t_ctx:.1f} · ③ {t_ref:.1f}]{RESET}")

    def _can_stream(self) -> bool:
        from .refiner import OllamaRefiner
        return (self.cfg.stream_output
                and isinstance(self.refiner, OllamaRefiner)
                and self.cfg.output_mode in ("paste", "type"))

    def _emit_final(self, text: str) -> str:
        """Inject a full (non-streamed) result in one shot."""
        if not text:
            return ""
        self._last_final = text
        out = text + " " if self.cfg.trailing_space else text
        self._suppress = True
        try:
            inject(out, self.cfg.output_mode)
        finally:
            self._suppress = False
        return text

    def _refine_streaming(self, raw, terms, context) -> str:
        """Type each sentence as the refiner produces it (forward-append)."""
        acc: list[str] = []

        def emit(sentence: str) -> None:
            s = self.dictionary.apply(sentence) if self.dictionary else sentence
            acc.append(s)
            inject(s, self.cfg.output_mode)

        self._suppress = True
        try:
            full, ok = self.refiner.refine_stream(raw, terms, context, emit)
            if not ok:
                # Empty or translated -> nothing was emitted; fall back safely.
                self._suppress = False
                return self._emit_final(self.refiner._fallback(raw))
            if self.cfg.trailing_space:
                inject(" ", self.cfg.output_mode)
        finally:
            self._suppress = False
        text = "".join(acc).strip()
        self._last_final = text
        return text

    # --- hotkey wiring ----------------------------------------------------
    def _on_toggle(self) -> None:
        if self._recording:
            self._stop_and_transcribe()
        else:
            self._start_recording()

    def run(self) -> None:
        self.load_model()
        key = self.cfg.hotkey
        mode = self.cfg.hotkey_mode

        print()
        print(f"{BOLD}Koe{RESET} is listening.")
        if mode == "toggle":
            print(f"  • Press {BOLD}{key}{RESET} to start, press again to stop & type.")
            self._install_hotkey(key, toggle=True)
        else:
            print(f"  • {BOLD}Hold {key}{RESET}, speak, release to type.")
            self._install_hotkey(key, toggle=False)
        cloud = self.refiner.name in ("claude", "openai")
        tag = f"{YELLOW}cloud{RESET}" if cloud else f"{GREEN}local{RESET}"
        print(f"  • Refiner(③) → {BOLD}{self.refiner.name}{RESET} [{tag}]")
        print(f"  • Output → {BOLD}{self.cfg.output_mode}{RESET} into the active window.")
        print(f"  • Press {BOLD}Esc Esc{RESET} (twice) or Ctrl+C to quit.")
        print(f"{DIM}Tip: edit config.json to change model, hotkey, language.{RESET}")
        print()

        try:
            # Double-Esc to quit so a single Esc still reaches your app.
            keyboard.add_hotkey("esc, esc", self._quit)
            keyboard.wait()
        except KeyboardInterrupt:
            pass
        finally:
            print(f"\n{DIM}Bye.{RESET}")

    def _install_hotkey(self, key: str, toggle: bool) -> None:
        """Robust push-to-talk via a low-level hook.

        keyboard.on_press_key/on_release_key are unreliable for modifier keys
        (left/right ctrl/alt/shift). A raw hook matched on BOTH the canonical name
        and the scan code captures them dependably and tells left from right.
        """
        # Allow re-installing at runtime (tray switches PTT <-> toggle).
        if self._hotkey_handle is not None:
            try:
                keyboard.unhook(self._hotkey_handle)
            except Exception:
                pass
            self._hotkey_handle = None
        if self._recording:  # don't strand an open stream when switching modes
            self._stop_and_transcribe()

        names = _key_aliases(key)
        try:
            scans = set(keyboard.key_to_scan_codes(key))
        except Exception:
            scans = set()

        def matches(e) -> bool:
            if self._suppress:
                return False
            nm = (e.name or "").lower()
            if nm in names:
                return True
            # Fallback only for keys that report no name; never for modifiers,
            # whose scan codes overlap (right ctrl shares code 29 with left ctrl).
            return (not nm) and e.scan_code in scans

        if toggle:
            held = [False]  # debounce key auto-repeat: toggle only on first press

            def on_event(e):
                if not matches(e):
                    return
                if e.event_type == keyboard.KEY_DOWN:
                    if not held[0]:
                        held[0] = True
                        self._on_toggle()
                elif e.event_type == keyboard.KEY_UP:
                    held[0] = False
        else:
            def on_event(e):
                if not matches(e):
                    return
                if e.event_type == keyboard.KEY_DOWN:
                    self._start_recording()  # guarded against key auto-repeat
                elif e.event_type == keyboard.KEY_UP:
                    self._stop_and_transcribe()

        self._hotkey_handle = keyboard.hook(on_event)

    # --- runtime controls used by the tray UI ----------------------------
    def set_hotkey_mode(self, mode: str) -> None:
        if mode not in ("toggle", "ptt") or mode == self.cfg.hotkey_mode:
            return
        self.cfg.hotkey_mode = mode
        self.cfg.save()
        self._install_hotkey(self.cfg.hotkey, toggle=(mode == "toggle"))

    def learn_correction(self, heard: str, correct: str) -> bool:
        """Improvement cycle: teach the dictionary a correction so it self-heals."""
        if not self.dictionary or not heard.strip() or not correct.strip():
            return False
        self.dictionary.learn(heard, correct)
        return True

    def open_dictionary(self) -> None:
        path = self.dictionary.path if self.dictionary else None
        if path:
            try:
                os.startfile(str(path))  # type: ignore[attr-defined]  # Windows
            except Exception:
                pass

    def _quit(self) -> None:
        print(f"\n{DIM}Quitting…{RESET}")
        keyboard.unhook_all()
        # Unblock keyboard.wait()
        os._exit(0)


def _key_aliases(hotkey: str) -> set[str]:
    """Names that should match a configured hotkey (lowercased).

    keyboard reports left/right modifiers with side-specific canonical names
    ("right ctrl", "left alt", …). We deliberately do NOT fold these into the
    bare name ("ctrl"), so that holding Right Ctrl never collides with the
    Left Ctrl we synthesize for Ctrl+V paste, nor with normal Left-Ctrl use.
    """
    base = hotkey.strip().lower()
    aliases = {base, base.split("+")[-1].strip()}
    synonyms = {
        "rctrl": "right ctrl", "right control": "right ctrl",
        "lctrl": "left ctrl", "left control": "left ctrl",
        "ralt": "right alt", "lalt": "left alt",
        "rshift": "right shift", "lshift": "left shift",
    }
    if base in synonyms:
        aliases.add(synonyms[base])
    return {a for a in aliases if a}


def _diagnose_keys() -> None:
    """Print every key event so we can confirm the exact name/scan_code a key
    reports on this machine. Useful when a chosen hotkey doesn't trigger."""
    print("Key diagnostic. Press the key you want to use as the hotkey a few")
    print("times (e.g. Right Ctrl). Watch the 'name' column — that's what to put")
    print("in config.json as \"hotkey\". Press Ctrl+C to stop.\n")
    print(f"{'event':<6} {'name':<14} scan_code")

    def show(e):
        et = "DOWN" if e.event_type == keyboard.KEY_DOWN else "up"
        print(f"{et:<6} {str(e.name):<14} {e.scan_code}")

    keyboard.hook(show)
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        pass


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv

    if "--list-devices" in argv:
        print(Recorder.list_devices())
        return

    if "--diagnose-keys" in argv:
        _diagnose_keys()
        return

    cfg = Config.load()

    # Allow a couple of quick CLI overrides without editing the file.
    for i, a in enumerate(argv):
        if a == "--model" and i + 1 < len(argv):
            cfg.model = argv[i + 1]
        elif a == "--language" and i + 1 < len(argv):
            cfg.language = argv[i + 1]
        elif a == "--device" and i + 1 < len(argv):
            cfg.device = argv[i + 1]

    # Default: system-tray shell. Use --console for the plain terminal loop.
    if "--console" in argv:
        KoeApp(cfg).run()
        return
    try:
        from . import tray
        tray.run(cfg)
    except Exception as exc:
        print(f"[tray unavailable: {exc}] falling back to console mode.")
        KoeApp(cfg).run()


if __name__ == "__main__":
    main()
