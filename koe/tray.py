"""System-tray shell for Koe (声).

A lightweight tray icon is the whole UI: it shows status on hover and its menu
lets the user change settings (trigger mode, model, ③ refiner) and run the
improvement cycle ("correct last output" → learns into the dictionary) — all
without editing config.json. This is the "simple .exe" form factor: no window,
lives in the tray, driven by the hotkey.
"""

from __future__ import annotations

import threading

from .app import KoeApp
from .config import Config


def _make_image(color: str):
    """A small round mic-dot icon drawn with Pillow."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, size - 6, size - 6), fill=color)
    # simple microphone glyph
    d.rounded_rectangle((26, 16, 38, 38), radius=6, fill="white")
    d.arc((22, 22, 42, 44), start=0, end=180, fill="white", width=3)
    d.line((32, 44, 32, 52), fill="white", width=3)
    d.line((24, 52, 40, 52), fill="white", width=3)
    return img


def _title(app: KoeApp) -> str:
    cloud = app.refiner.name in ("claude", "openai")
    where = "cloud" if cloud else "local"
    return (f"Koe · {app.cfg.hotkey}/{app.cfg.hotkey_mode} · "
            f"③{app.refiner.name}({where}) · {app._status_text}")


def _status_color(app: KoeApp) -> str:
    s = app._status_text
    if s.startswith("recording"):
        return "#e03131"   # red
    if s.startswith("transcribing") or s.startswith("loading"):
        return "#f08c00"   # amber
    return "#1c7ed6"       # blue (idle/ready)


def _correct_dialog(app: KoeApp) -> None:
    """Improvement UI: ask which word was misheard and its correct form, then
    teach the dictionary so the same mistake self-heals next time."""
    import tkinter as tk
    from tkinter import simpledialog, messagebox

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        last = app._last_final or "(まだ何も入力していません)"
        heard = simpledialog.askstring(
            "Koe — 改善学習",
            f"直前の出力:\n{last}\n\n誤って認識された語（部分でOK）:",
            parent=root,
        )
        if heard:
            correct = simpledialog.askstring(
                "Koe — 改善学習",
                f"「{heard}」の正しい表記:",
                parent=root,
            )
            if correct and app.learn_correction(heard, correct):
                messagebox.showinfo(
                    "Koe",
                    f"学習しました：「{heard}」→「{correct}」\n"
                    f"次回から自動修正されます（dictionary.txt に保存）。",
                    parent=root,
                )
    finally:
        root.destroy()


def _build_menu(app: KoeApp, refresh):
    from pystray import Menu, MenuItem as Item

    def mode(label, value):
        return Item(label, lambda i, it: (app.set_hotkey_mode(value), refresh()),
                    checked=lambda it: app.cfg.hotkey_mode == value, radio=True)

    def model(label, value):
        return Item(label, lambda i, it: (app.reload_engine(value), refresh()),
                    checked=lambda it: app.cfg.model == value, radio=True)

    def refiner(label, value):
        return Item(label, lambda i, it: (app.reload_refiner(value), refresh()),
                    checked=lambda it: app.cfg.refiner_backend == value, radio=True)

    def omodel(label, value):
        return Item(label, lambda i, it: (app.set_ollama_model(value), refresh()),
                    checked=lambda it: app.cfg.ollama_model == value, radio=True)

    return Menu(
        Item(lambda it: _title(app), None, enabled=False),
        Menu.SEPARATOR,
        Item("トリガー方式", Menu(
            mode("トグル（1回押し 開始/停止）", "toggle"),
            mode("押しっぱなし（PTT）", "ptt"),
        )),
        Item("モデル（②文字起こし）", Menu(
            model("small（軽量・高速）", "small"),
            model("large-v3-turbo（推奨）", "large-v3-turbo"),
            model("large-v3（高精度）", "large-v3"),
        )),
        Item("補正③（文脈整形）", Menu(
            refiner("rules（LLMなし・最速）", "rules"),
            refiner("ollama（ローカルLLM・推奨）", "ollama"),
            refiner("claude（クラウド・要APIキー）", "claude"),
            refiner("openai（クラウド・要APIキー）", "openai"),
        )),
        Item("③ローカルモデル速度", Menu(
            omodel("qwen2.5:3b（高速）", "qwen2.5:3b"),
            omodel("qwen2.5:7b（高品質・推奨）", "qwen2.5:7b"),
        )),
        Menu.SEPARATOR,
        Item("辞書を開く（dictionary.txt）", lambda i, it: app.open_dictionary()),
        Item("直前の出力を修正（学習）…", lambda i, it: _correct_dialog(app)),
        Menu.SEPARATOR,
        Item("終了", lambda i, it: _quit(i, app)),
    )


def _quit(icon, app: KoeApp) -> None:
    try:
        icon.stop()
    except Exception:
        pass
    app._quit()


def run(cfg: Config) -> None:
    import pystray

    app = KoeApp(cfg)

    icon = pystray.Icon("koe", _make_image(_status_color(app)), "Koe")

    def refresh():
        try:
            icon.title = _title(app)
            icon.icon = _make_image(_status_color(app))
            icon.update_menu()
        except Exception:
            pass

    app.on_status_change = lambda _s: refresh()
    icon.menu = _build_menu(app, refresh)

    # Hotkey is live immediately; recording is gated until the model is ready.
    app._install_hotkey(cfg.hotkey, toggle=(cfg.hotkey_mode == "toggle"))
    threading.Thread(target=app.load_model, daemon=True).start()

    icon.run()  # blocks on the main thread until quit
