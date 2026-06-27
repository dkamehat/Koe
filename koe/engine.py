"""Local transcription engine built on faster-whisper (CTranslate2).

Everything runs on-device. The model is downloaded once from Hugging Face on the
first run and cached under ~/.cache/huggingface; after that it works fully offline.
"""

from __future__ import annotations

import os
import sys

import numpy as np


def _ensure_cuda_dlls_on_path() -> None:
    """Make the pip-installed cuBLAS/cuDNN DLLs discoverable on Windows.

    faster-whisper's CUDA backend needs these DLLs; when they come from the
    nvidia-*-cu12 wheels they live inside site-packages and aren't on PATH.
    """
    if os.name != "nt":
        return
    try:
        import nvidia  # type: ignore

        base = os.path.dirname(nvidia.__file__)
    except Exception:
        return
    for sub in ("cublas/bin", "cudnn/bin"):
        p = os.path.join(base, *sub.split("/"))
        if os.path.isdir(p):
            os.add_dll_directory(p)
            os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")


class TranscriptionEngine:
    def __init__(
        self,
        model: str = "large-v3-turbo",
        device: str = "auto",
        compute_type: str = "auto",
        language: str | None = None,
    ):
        _ensure_cuda_dlls_on_path()
        from faster_whisper import WhisperModel  # imported late so DLL path is set

        self.requested_device = device
        device, compute_type = self._resolve(device, compute_type)
        self.device = device
        self.compute_type = compute_type
        self.language = None if (language in (None, "", "auto")) else language

        try:
            self.model = WhisperModel(model, device=device, compute_type=compute_type)
        except Exception as exc:  # CUDA missing/misconfigured -> fall back to CPU
            if device == "cuda":
                print(
                    f"[engine] CUDA load failed ({exc}); falling back to CPU.",
                    file=sys.stderr,
                )
                self.device, self.compute_type = "cpu", "int8"
                self.model = WhisperModel(
                    model, device="cpu", compute_type="int8"
                )
            else:
                raise

    @staticmethod
    def _resolve(device: str, compute_type: str) -> tuple[str, str]:
        if device == "auto":
            device = "cuda" if _cuda_available() else "cpu"
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        return device, compute_type

    def transcribe(self, audio: np.ndarray, initial_prompt: str | None = None) -> str:
        if audio.size == 0:
            return ""
        segments, _info = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=5,
            # Fall back to higher temperatures only if the greedy pass looks
            # unreliable (low logprob / high gzip ratio) — keeps clean audio
            # deterministic while rescuing hard segments.
            temperature=[0.0, 0.2, 0.4, 0.6],
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
            # Bias decoding toward the user's terminology (proper nouns / jargon).
            initial_prompt=initial_prompt,
            vad_filter=True,  # trims silence / breath gaps for cleaner output
            vad_parameters={"min_silence_duration_ms": 300},
            condition_on_previous_text=False,
        )
        return "".join(seg.text for seg in segments).strip()


def _cuda_available() -> bool:
    _ensure_cuda_dlls_on_path()
    try:
        from ctranslate2 import get_cuda_device_count

        return get_cuda_device_count() > 0
    except Exception:
        return False
