# Koe 声 🎙️

**Windows 用の、ローカル完結・オフライン音声入力ツール。** キーを押して（または1回タップして）
話すと、整形済みのテキストがフォーカス中のアプリに入力されます。音声は**あなたのPCの中だけ**で
文字起こしされ、**クラウドには一切送信されません。**

<p align="center"><img src="docs/demo.gif" alt="Koe デモ — Right Ctrl を押して話すと整形済みテキストが入る" width="720"></p>

> テーゼ：**誰でも使えて、誰でも安全。** 無料・オフライン・アカウント不要・テレメトリなし。
> [Aqua Voice](https://withaqua.com/) に着想を得て、100%ローカルで動くように作り直しました。

- 🔒 **100%ローカル** — クラウドなし・APIキーなし・テレメトリなし。初回のモデルDL後はオフライン動作。
- ⚡ **高速** — `faster-whisper`（CTranslate2）をCUDAで実行。まともなNVIDIA GPUならほぼリアルタイム。
- 🌐 **日本語 / 英語** — Whisper `large-v3` 系が日英を自動判定して書き起こし。
- 🧹 **控えめで賢い整形** — ローカルLLM（Ollama経由）がフィラーを除去し句読点を付与。**あなたの言い回しは保持**（翻訳もしない）。
- 🧠 **文脈グラウンディング** — フォーカス中のウィンドウ/欄をローカルで読み、固有名詞やコード変数名を正しく綴る。
- 📚 **自己修復辞書** — 一度修正を教えれば、その語は以後ずっと自動修正。
- 🎚️ **プリロール録音** — 常時オンのリングバッファでキーを押す**直前**の音も前置 → 頭欠け（「…ello」→「Hello」）を解消。
- ⌨️ **どこにでも入力** — どんなWindowsアプリにもUnicode安全に流し込み。

## 必要環境

- **Windows 10/11**
- **Python 3.12**
- **NVIDIA GPU**（強く推奨。無くてもCPUに自動フォールバックするが遅い）
- *(任意)* **[Ollama](https://ollama.com)** — ローカルLLMによる整形レイヤー用

## インストール

### 方法A — アプリをダウンロード（Python も git も不要）

1. [最新リリース](https://github.com/dkamehat/Koe/releases/latest)から **`Koe-win64-cuda.zip`** を入手。
2. 好きな場所に解凍。
3. **`Koe.exe`** をダブルクリック。初回だけWhisperモデルをDLし、以降はオフライン動作。

`config.json` と `dictionary.txt` は `Koe.exe` の隣に作られるので、フォルダごと持ち運べます。

### 方法B — ソースから実行（開発者向け）

```powershell
git clone https://github.com/dkamehat/Koe.git
cd Koe
.\setup.ps1
```

`setup.ps1` が `.venv` を作り、CUDAライブラリ含め全依存を入れます（約1〜2GB）。

自分でアプリをビルドするなら：`.\build.ps1` → `dist\Koe\Koe.exe`。

*(任意・推奨)* ローカルLLM整形を有効化：

```powershell
# https://ollama.com から Ollama を入れて：
ollama pull qwen2.5:7b
```

## 起動

```powershell
.\run-admin.bat        # 管理者で起動（全アプリでグローバルキーが効く）
```

初回だけWhisperモデルをDL（`~/.cache/huggingface` にキャッシュ）し、以降は完全オフライン。
タスクトレイにマイクアイコンが出ます（Windows 11では `^`（隠れているアイコン）の中）。

**使い方：** 既定の**トグル**方式では、**Right Ctrl を1回**押して開始 → 話す（途中で考えて止まってもOK）
→ もう1回押して確定 → 整形済みテキストがフォーカス中のアプリに入ります。設定はトレイアイコンを右クリック。
ターミナルで動かすなら `run.py --console`。

## ③ Refiner — 文脈を読む整形（差し替え可能・ローカル優先）

| `refiner_backend` | 内容 | プライバシー / コスト |
|-------------------|------|----------------------|
| `auto`（既定）    | Ollamaが起動中ならそれを、無ければ `rules` | 100%ローカル |
| `rules`           | LLMなしの決定的整形 | 100%ローカル・即時 |
| `ollama`          | GPU上のローカルLLM（例 `qwen2.5:7b`） | **100%ローカル・無料** |
| `claude`          | Anthropic API | クラウド・従量・`ANTHROPIC_API_KEY` 必要 |
| `openai`          | OpenAI API | クラウド・従量・`OPENAI_API_KEY` 必要 |

**安全設計：** クラウドは明示選択時のみ使用。APIキーは**環境変数からのみ**読み、`config.json` には
書きません（共有してもキーは漏れない）。**クラウド補正を選ぶと文脈グラウンディングは自動で無効化**され、
画面のテキストが外部へ出ることはありません。

> ChatGPT/Claude の**サブスクと開発者APIは別課金**です。ゼロ円＋高精度なら、ローカルの **`ollama`** を。

## 辞書と改善ループ

固有名詞・専門用語を `dictionary.txt`（初回起動時に生成。`dictionary.txt.example` 参照）に書くと
正しく書き起こされます。誤変換が出たら、トレイの **「直前の出力を修正（学習）…」** から
「聞こえた語 → 正しい語」を入れるだけで、以後ずっと自動修正されます（すべて端末内で完結）。

## 自分の声で品質を測る（`bench.py`）

「納得できる品質」は人それぞれなので、勘で判断せず**比較可能**にします：

```powershell
python bench.py record "納得できる正解テキスト"        # サンプル録音（Enterで停止）
python bench.py run                                    # 全サンプルを採点（CER＋差分表示）
python bench.py run --model large-v3 --refiner rules   # config を触らず即A/B
```

サンプルは `./bench/` に保存（gitignore済）＝あなたの声は端末外に出ません。
指標（正規化CER）・バージョン別結果・土台モデルの公開日本語CERとの関係は
**[BENCHMARK.md](BENCHMARK.md)** を参照。

## システム音声のライブ字幕（`interpreter.py`）

Koe Interpreter は、スピーカーで再生中の音声（会議・動画・通話）を、同じローカル
エンジンで字幕化します。音声は一切端末外に出ません。

```powershell
python interpreter.py            # 既定スピーカーをWASAPIループバックで字幕化
python interpreter.py --list     # キャプチャ可能なスピーカー一覧
python interpreter.py --to ja     # 字幕を日本語へ翻訳（en/zh/ko/… も可・ローカルollama）
python interpreter.py --to ja --suggest  # F9 で「返すべき返事」を提案（＋日本語訳）
python interpreter.py --to ja --auto-suggest  # 質問を検知したら返信案を自動で下に表示
python interpreter.py --to ja --ollama-model qwen2.5:14b  # 翻訳を強いモデルで（要 ollama pull）
python interpreter.py --translate # Whisper内蔵の高速翻訳 → 英語のみ
python interpreter.py --debug    # RMSメーターで --threshold を調整
```

faster-whisper は逐次ストリーミング非対応なので、無音の切れ目で発話を区切って
発話単位で文字起こしします（話者の自然な間に追従）。`--to <言語>` を付けると、
各字幕を（辞書整形と同じ）ローカルの Ollama サーバーで翻訳し、原文＋訳文を表示
します（端末外には一切出ません。Ollama 起動が必要）。`--suggest` を付けると、
外国語の通話中に F9 を押すだけで「返すべき返事」を相手の言語で提案＋自分の言語の
訳も表示します。`--role "..."` で状況を、`--context <ファイル>` で事前資料（経歴・
求人票・会議アジェンダ等）を読み込ませると、その内容に沿った回答になります。停止は Ctrl+C。

翻訳品質を上げるには `--ollama-model qwen2.5:14b` で通訳側だけ強いモデルを使えます
（口述筆記は軽いモデルのまま）。7B では日本語出力に中国語が混じることがありますが、
14B ではほぼ解消します。

## 仕組み

```
 マイク ─► 録音 ─► faster-whisper ─► 辞書 ─► ③整形 ─► クリップボード貼付 ─► アプリ
            ①        ②(CUDA)       固有名詞  ローカルLLM   Unicode安全        ④
                                            (文脈反映・ストリーミング)
```

## トラブルシューティング

- **キーが効かない** → `run-admin.bat`（管理者）で起動。
- **マイクが違う** → `python run.py --list-devices` で番号を確認し `config.json` の `input_device` に設定。
- **ホットキーが反応しない** → `python run.py --diagnose-keys` でキーを押し、表示名を使う。
- **CUDAロード失敗** → 自動でCPU（`int8`）にフォールバック。`model: "small"` も試す。
- **貼り付かない** → 一部アプリは自動貼付を弾く。`output_mode: "type"` に。

## クレジット & ライセンス

[Aqua Voice](https://withaqua.com/) に着想。[faster-whisper](https://github.com/SYSTRAN/faster-whisper)
と [Ollama](https://ollama.com) を使用。Aqua Voice とは無関係（非提携）。

MIT — [LICENSE](LICENSE) 参照。
