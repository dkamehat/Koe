"""First-run setup wizard: a friendly 4-step window a non-developer sees the
first time Koe starts, and can re-open any time with `python run.py --setup`.

Explains what Koe is and that everything stays on the machine (thesis: local
by default), detects the GPU and recommends a Whisper model, lets the user
pick and TEST their microphone with a live level meter, and explains the
hotkey — then writes the choices into the existing Config fields (`model`,
`input_device`, `hotkey_mode`) and saves. No new config keys.

Pure helpers live at module scope (CI-testable, no display/mic needed); the
interactive window lives in `run_wizard` / `_Wizard`, whose tkinter/sounddevice
imports stay lazy inside functions (invariant 2) — this module must import
cleanly on Ubuntu CI with neither package installed.
"""

from __future__ import annotations

import queue
import sys
import threading


# --- pure helpers (module scope: stdlib only, CI-testable) ------------------

def recommend_model(cuda_available: bool) -> tuple[str, str]:
    """(model_name, ja_rationale). CUDA -> ("large-v3-turbo", "GPUを検出しました。
    最高精度の推奨モデルです（初回 約1.6GB をダウンロード）").
    No CUDA -> ("small", "GPUが見つからないため、CPUでも快適な軽量モデルを
    推奨します（初回 約500MB）。精度優先なら後から変更できます").
    WHY in comment: turbo beat large-v3 on the owner's bench (BENCHMARK v0);
    small is the documented CPU fallback in README troubleshooting."""
    if cuda_available:
        # large-v3-turbo beat large-v3 on the owner's own bench (BENCHMARK
        # v0) — best accuracy/speed tradeoff on real GPU hardware, so it's
        # the GPU recommendation, not simply "the biggest model". ~1.6GB is
        # the actual Hugging Face download size of this model's weights.
        return (
            "large-v3-turbo",
            "GPUを検出しました。最高精度の推奨モデルです"
            "（初回 約1.6GB をダウンロード）",
        )
    # "small" is the documented CPU fallback in README troubleshooting: it
    # stays responsive without a GPU. ~500MB is its actual download size —
    # small enough that the first-launch wait is short on a slow line.
    return (
        "small",
        "GPUが見つからないため、CPUでも快適な軽量モデルを"
        "推奨します（初回 約500MB）。精度優先なら後から変更できます",
    )


def input_device_choices(
    devices: list[dict], default_index: int | None
) -> list[tuple[int | None, str]]:
    """Parse sounddevice.query_devices()-shaped dicts into dropdown choices:
    first entry always (None, "既定のマイク（Windows設定に従う）"); then every
    device with max_input_channels > 0 as (index, f"{index}: {name}"), marking
    the default with " ← 既定". Pure: takes plain dicts, so tests feed fakes."""
    choices: list[tuple[int | None, str]] = [
        (None, "既定のマイク（Windows設定に従う）")
    ]
    for index, device in enumerate(devices):
        if device.get("max_input_channels", 0) <= 0:
            continue
        label = f"{index}: {device.get('name', '')}"
        if index == default_index:
            label += " ← 既定"
        choices.append((index, label))
    return choices


def level_fraction(rms: float) -> float:
    """Mic RMS -> 0.0..1.0 meter fill. Normal speech RMS is ~0.03–0.1
    (koe/recorder.py's meter docs), so full scale at 0.15 keeps the bar lively
    without pegging: min(1.0, rms / 0.15)."""
    # 0.15 full-scale: normal speech RMS (~0.03-0.1, see Recorder.level's
    # docstring) fills most of the bar, but a loud voice still pegs cleanly
    # at 1.0 instead of the bar looking broken/frozen near the top.
    return min(1.0, rms / 0.15)


# --- wizard entry point (all heavy imports inside) --------------------------

def run_wizard(cfg) -> bool:
    """Show the 4-step wizard, mutate+save cfg on finish. Returns True if the
    user completed it, False if cancelled/unavailable — the caller proceeds
    with defaults either way (the wizard must never block startup — D16).
    tkinter import failure, any Exception during construction: print one
    stderr line ('! setup wizard unavailable (...) — starting with defaults')
    and return False."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:
        print(f"! setup wizard unavailable ({exc}) — starting with defaults",
              file=sys.stderr)
        return False

    try:
        wizard = _Wizard(tk, ttk, cfg)
        wizard.run()
        return wizard.completed
    except Exception as exc:
        print(f"! setup wizard unavailable ({exc}) — starting with defaults",
              file=sys.stderr)
        return False


# Step 2 shows the recommended model plus these two others (no "medium" — out
# of scope, config.json comments cover it for anyone who wants it).
MODEL_CHOICES = ("small", "large-v3-turbo", "large-v3")
MODEL_DESCRIPTIONS = {
    "small": "軽量・高速。CPUでも快適に動作します。",
    "large-v3-turbo": "速度と精度のバランスが良い最新モデル。",
    "large-v3": "最も高精度ですが、処理はやや低速です。",
}


class _Wizard:
    """Owns the Tk root and all 4 step-frames. MUST be constructed and driven
    (run()) from the main thread — tkinter is main-thread-only, same
    constraint as koe/overlay.py's run_overlay."""

    WELCOME_STEP, MODEL_STEP, MIC_STEP, HOTKEY_STEP = range(4)

    def __init__(self, tk_module, ttk_module, cfg):
        self._tk = tk_module
        self._ttk = ttk_module
        self.cfg = cfg
        self.completed = False

        self.root = tk_module.Tk()
        self.root.title("Koe — 初回セットアップ")
        # Fixed size: a short, linear 4-step wizard doesn't need resizing.
        self.root.geometry("520x420")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.model_var = tk_module.StringVar()
        self.mode_var = tk_module.StringVar(value=cfg.hotkey_mode)
        self._device_var = tk_module.StringVar()
        self._device_choices: list[tuple[int | None, str]] = []

        # GPU probe plumbing: _cuda_available() imports ctranslate2, which can
        # take seconds — must run off the Tk mainloop thread (known pitfall).
        self._cuda_queue: queue.Queue = queue.Queue()
        self._cuda_available = False
        self._gpu_label = None

        # Mic step plumbing: one-slot box the InputStream callback writes into;
        # the UI thread polls it via after(), so no Tk object is ever touched
        # from another thread.
        self._mic_stream = None
        self._mic_box = {"rms": 0.0}
        self._level_bar = None

        self._summary_label = None
        self.frames: dict[int, object] = {}
        self.step_index = self.WELCOME_STEP

        self._build_nav()
        self._show_step(self.WELCOME_STEP)

    def run(self) -> None:
        self.root.mainloop()

    # --- navigation ----------------------------------------------------
    def _build_nav(self) -> None:
        tk = self._tk
        nav = tk.Frame(self.root)
        nav.pack(side="bottom", fill="x", padx=16, pady=(0, 16))
        self._back_btn = tk.Button(nav, text="戻る", width=10, command=self._on_back)
        self._back_btn.pack(side="left")
        self._next_btn = tk.Button(nav, text="次へ", width=12, command=self._on_next)
        self._next_btn.pack(side="right")

    def _on_back(self) -> None:
        if self.step_index == self.WELCOME_STEP:
            return
        self._leave_step(self.step_index)
        self._show_step(self.step_index - 1)

    def _on_next(self) -> None:
        if self.step_index == self.HOTKEY_STEP:
            self._finish()
            return
        self._leave_step(self.step_index)
        self._show_step(self.step_index + 1)

    def _leave_step(self, index: int) -> None:
        if index == self.MIC_STEP:
            self._close_mic_stream()

    def _show_step(self, index: int) -> None:
        if index not in self.frames:
            builder = {
                self.WELCOME_STEP: self._build_step_welcome,
                self.MODEL_STEP: self._build_step_model,
                self.MIC_STEP: self._build_step_mic,
                self.HOTKEY_STEP: self._build_step_hotkey,
            }[index]
            self.frames[index] = builder()
        for frame in self.frames.values():
            frame.pack_forget()
        self.frames[index].pack(fill="both", expand=True, padx=16, pady=16)
        self.step_index = index
        self._back_btn.config(state=("disabled" if index == self.WELCOME_STEP else "normal"))
        self._next_btn.config(text=("Koeを開始" if index == self.HOTKEY_STEP else "次へ"))
        if index == self.MIC_STEP:
            self._open_mic_stream()
        if index == self.HOTKEY_STEP:
            self._refresh_summary()

    def _on_close(self) -> None:
        # Closing the window = cancel: nothing saved, caller proceeds with
        # whatever cfg already had (D16 — the wizard must never block startup).
        self._close_mic_stream()
        self.completed = False
        self.root.destroy()

    def _finish(self) -> None:
        self.cfg.model = self.model_var.get() or self.cfg.model
        self.cfg.input_device = self._selected_device_index()
        self.cfg.hotkey_mode = self.mode_var.get()
        self.cfg.save()
        self.completed = True
        self._close_mic_stream()
        self.root.destroy()

    # --- step 1: ようこそ -------------------------------------------------
    def _build_step_welcome(self):
        tk = self._tk
        frame = tk.Frame(self.root)
        tk.Label(
            frame, text="Koe（声）へようこそ。",
            font=("Yu Gothic UI", 12, "bold"), justify="left", anchor="w",
        ).pack(fill="x", pady=(0, 4))
        tk.Label(
            frame,
            text=("Koeはオフラインで動く音声入力・翻訳・会話ツールです。\n"
                  "音声はこのPCの中だけで処理され、クラウドには一切送信されません。"),
            justify="left", anchor="w", wraplength=460,
        ).pack(fill="x", pady=(0, 16))
        self._gpu_label = tk.Label(frame, text="GPUを確認中…", justify="left", anchor="w")
        self._gpu_label.pack(fill="x")
        self._start_gpu_probe()
        return frame

    def _start_gpu_probe(self) -> None:
        def probe():
            try:
                from .engine import _cuda_available
                result = _cuda_available()
            except Exception:
                result = False
            self._cuda_queue.put(result)

        threading.Thread(target=probe, daemon=True).start()
        self.root.after(200, self._poll_gpu_probe)

    def _poll_gpu_probe(self) -> None:
        try:
            result = self._cuda_queue.get_nowait()
        except queue.Empty:
            self.root.after(200, self._poll_gpu_probe)
            return
        self._cuda_available = result
        text = "NVIDIA GPU: 検出 (CUDA)" if result else "NVIDIA GPU: なし（CPUで動作します）"
        if self._gpu_label is not None:
            self._gpu_label.config(text=text)
        # The model step bakes the recommendation in when built — if the user
        # rushed past the welcome step before the probe finished, a cached
        # frame would keep recommending the CPU model on a GPU machine. Drop
        # the cache so the next visit rebuilds with the real result (never
        # yank the frame out from under the user mid-view).
        if self.step_index != self.MODEL_STEP:
            self.frames.pop(self.MODEL_STEP, None)

    # --- step 2: モデル ----------------------------------------------------
    def _build_step_model(self):
        tk = self._tk
        frame = tk.Frame(self.root)
        tk.Label(
            frame, text="使用するWhisperモデルを選んでください。",
            justify="left", anchor="w",
        ).pack(fill="x", pady=(0, 8))

        recommended, rationale = recommend_model(self._cuda_available)
        self.model_var.set(recommended)

        for name in MODEL_CHOICES:
            suffix = "（推奨)" if name == recommended else ""
            text = f"{name}{suffix} — {MODEL_DESCRIPTIONS[name]}"
            tk.Radiobutton(
                frame, text=text, value=name, variable=self.model_var,
                justify="left", anchor="w",
            ).pack(fill="x", anchor="w")

        tk.Label(
            frame, text=rationale, justify="left", anchor="w", wraplength=460,
        ).pack(fill="x", pady=(8, 8))
        tk.Label(
            frame,
            text="最初の起動時に一度だけモデルをダウンロードします（以後オフライン動作)",
            justify="left", anchor="w", wraplength=460,
        ).pack(fill="x")
        return frame

    # --- step 3: マイク ----------------------------------------------------
    def _build_step_mic(self):
        tk = self._tk
        ttk = self._ttk
        frame = tk.Frame(self.root)
        tk.Label(
            frame, text="使用するマイクを選んでください。",
            justify="left", anchor="w",
        ).pack(fill="x", pady=(0, 8))

        devices: list[dict] = []
        default_index = None
        try:
            import sounddevice as sd
            devices = list(sd.query_devices())
            default_in = sd.default.device[0]
            default_index = default_in if default_in is not None and default_in >= 0 else None
        except Exception:
            tk.Label(
                frame,
                text="マイク一覧を取得できませんでした — 既定のマイクを使用します",
                justify="left", anchor="w", wraplength=460,
            ).pack(fill="x", pady=(0, 8))

        self._device_choices = input_device_choices(devices, default_index)
        labels = [label for _, label in self._device_choices]
        selected_label = labels[0] if labels else ""
        for index, label in self._device_choices:
            if index == self.cfg.input_device:
                selected_label = label
                break
        self._device_var.set(selected_label)

        dropdown = ttk.Combobox(
            frame, textvariable=self._device_var, values=labels,
            state="readonly", width=48,
        )
        dropdown.pack(fill="x", pady=(0, 8))
        dropdown.bind("<<ComboboxSelected>>", self._on_device_change)

        self._level_bar = ttk.Progressbar(
            frame, orient="horizontal", mode="determinate", maximum=100,
        )
        self._level_bar.pack(fill="x", pady=(8, 4))
        tk.Label(
            frame, text="マイクに向かって話すとバーが動きます",
            justify="left", anchor="w",
        ).pack(fill="x")
        return frame

    def _selected_device_index(self):
        current = self._device_var.get()
        for index, label in self._device_choices:
            if label == current:
                return index
        return None

    def _on_device_change(self, event=None) -> None:  # noqa: ARG002
        self._close_mic_stream()
        self._open_mic_stream()

    def _open_mic_stream(self) -> None:
        self._close_mic_stream()
        try:
            import sounddevice as sd
            self._mic_stream = sd.InputStream(
                samplerate=16000, channels=1, dtype="float32",
                device=self._selected_device_index(),
                callback=self._mic_callback,
                blocksize=0,
            )
            self._mic_stream.start()
        except Exception:
            # A busy/misconfigured mic must never kill the wizard — the meter
            # just stays flat and the user can still pick a different device.
            self._mic_stream = None
        self.root.after(100, self._poll_mic_level)

    def _close_mic_stream(self) -> None:
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None

    def _mic_callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        import numpy as np  # lazy: heavy import, only needed while this step is live
        chunk = indata.reshape(-1)
        if chunk.size:
            self._mic_box["rms"] = float(np.sqrt(np.mean(chunk * chunk)))

    def _poll_mic_level(self) -> None:
        if self.step_index != self.MIC_STEP:
            return  # left the step; the stream is already closed, stop polling
        if self._level_bar is not None:
            self._level_bar["value"] = level_fraction(self._mic_box["rms"]) * 100
        self.root.after(100, self._poll_mic_level)

    # --- step 4: ホットキーと完了 --------------------------------------------
    def _build_step_hotkey(self):
        tk = self._tk
        frame = tk.Frame(self.root)
        tk.Label(
            frame,
            text=f"{self.cfg.hotkey} を1回押して話し、もう一度押すと文字になります",
            justify="left", anchor="w", wraplength=460,
        ).pack(fill="x", pady=(0, 12))

        tk.Radiobutton(
            frame, text="トグル（推奨）— 1回押して開始、もう一度押して終了",
            value="toggle", variable=self.mode_var, justify="left", anchor="w",
        ).pack(fill="x", anchor="w")
        tk.Radiobutton(
            frame, text="プッシュ・トゥ・トーク — 押している間だけ録音",
            value="ptt", variable=self.mode_var, justify="left", anchor="w",
        ).pack(fill="x", anchor="w")

        self._summary_label = tk.Label(
            frame, text="", justify="left", anchor="w", wraplength=460,
        )
        self._summary_label.pack(fill="x", pady=(16, 0))
        return frame

    def _refresh_summary(self) -> None:
        model = self.model_var.get() or self.cfg.model
        device_label = self._device_var.get() or "既定のマイク"
        mode_label = "トグル" if self.mode_var.get() == "toggle" else "プッシュ・トゥ・トーク"
        if self._summary_label is not None:
            self._summary_label.config(
                text=f"モデル: {model}\nマイク: {device_label}\nモード: {mode_label}"
            )
