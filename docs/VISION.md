# Koe Vision — AIとの「本当に逐次的な会話」へ

*(English summary at the bottom. This document is the project's north star:
read it before proposing any new feature, and update it when the ideal itself
evolves — not for every implementation detail.)*

## 理想（北極星）

> **AIとのやり取りを、本当に逐次的な会話で実現する。**

チャット欄に文章を書いて送信し、まとまった返答を待つ——それは「文書の交換」で
あって会話ではない。目指すのは、人と話すときと同じリズムでAIと話せること：

1. **間（ま）を理解する** — 沈黙の長さではなく「何を言ったか」でターンが終わる。
   「…ですか？」なら即座に答え、「…けど」なら考え終わるまで待つ。
2. **遮れる・遮られる** — AIの返答が的外れなら途中で遮れて、AIは即座に黙る。
   遮られたAIは「途中で切られた」ことを理解して続きの会話をする。
3. **応答は音声で、1秒台で返る** — 返答の「最初の一言」が短く速く返り、
   残りは話しながら生成される（文単位ストリーミング）。
4. **会話が成果物になる** — 話した結果は消えない。「貼って」の一言で、会話の
   結論がフォーカス中のアプリにテキストとして入る（会話＝入力手段）。
5. **すべてローカル** — 上のすべてがクラウドなしで動く。これは制約ではなく
   優位性：0円・低遅延・完全プライベートだからこそ、相槌や常駐のような
   「贅沢な」会話行動が実装できる。

## なぜ Koe でやるのか

Koe には会話ループの部品がすでに全部ある。①マイク/ループバック捕捉、
②faster-whisper、辞書バイアス、③ローカルLLM（Ollama）と文境界ストリーミング
（`_find_boundary`）、VAD自動較正、`--role`/`--context` によるグラウンディング。
欠けていたのは ④声（ローカルTTS）と、**ターンテイキングの頭脳**だけだった。
Koe Talk はその2つを足して部品を会話に組み上げたもの——新規発明ではなく
**純粋な合成**であり、それがこのリポジトリの文化（D15: 純ロジックの核＋端のI/O）
に沿う進み方である。

## インタラクション・モデル（Koe Talk）

```
 あなた ─ 話す ─ 間 ─┐                       ┌─ 話す（続き/遮り）─ …
                      ▼                       ▼
 Koe   [LISTENING] → 意味論的な発話終端検出 → [THINKING] → [SPEAKING]
        断片STTが間の裏で走る    「…けど」= 待つ   LLM文単位 → TTS文単位
                                「…ですか？」= 即答  （エポックでいつでも破棄可能）
```

- **意味論的エンドポインティング** — 発話は短い断片（0.4秒の無音）で切られ即座に
  文字化されるので、ユーザーが黙った時点で「何と言ったか」は既知。語尾で待ち時間が
  変わる（質問 0.45s / 完結 0.65s / 中立 1.0s / 継続 2.0s。`talk_patience` で一括
  スケール）。誤判定の非対称性が設計原則：**「未完」と誤るコストは約1秒の忍耐、
  「完結」と誤るコストは思考の中断と信頼**。迷ったら待つ。
- **エポック・キャンセル** — 返答の生成・合成・再生はすべてエポック番号を持ち、
  ユーザーが割り込んだらエポックを進めるだけで全段が自然に停止する。スレッドを
  殺さない、フラグを乱立させない、競合しない。
- **履歴は「実際に声に出た文」だけ** — 5文生成しても2文で遮られたら、モデルは
  「2文しか言っていない＋（途中で遮られた）」と記憶する。遮られた人間と同じ
  振る舞いが返ってくる。
- **エコーの梯子** — 既定 `mute`（AI発話中はマイク無効：スピーカーでも構造的に
  エコー不能）→ `headphones`（マイク常時オン、0.3秒の連続音声で即遮り）→
  将来: 較正ハンドシェイク＋自己エコー照合（v2）。AECは採らない（D23）。
- **会話＝入力手段** — 「貼って」で直前の返答が `koe/injector` 経由でフォーカス
  中のアプリに入る。口述筆記（第一の柱）と会話（第三の柱）はここで合流する。

## この形が解放する新しい使い方

- **壁打ち・聞き役** — 考えごとを声に出す相手。将来の相槌（v2）と組み合わせ、
  「主に黙って聞き、聞かれたら答える」AIはローカルだからこそ成立する
  （相槌1回のコストが0円・0msである必要がある）。
- **英会話・面接練習** — `--role "面接官。厳しめに深掘りして"` ＋
  `--context resume.md`。本物の中断力学（遮れる・待ってくれる）を持つ練習相手は
  クラウド製品にもない。発話終端の忍耐は語学学習者にこそ効く。
- **口述の対話的推敲** — 書いた文章について話し、結論を「貼って」で流し込む。
  将来は「もっと丁寧に」で直前の口述筆記そのものを声で修正する（v2+）。
- **Interpreter の三者会話化** — 通訳（第二の柱）は既に返答を「提案」する。
  次の段階はそれを**あなたの声として通話に出す**こと（仮想マイク）。字幕ツール
  が会話代理人になる（v3）。
- **Koe 自体のハンズフリー操作** — モデル切替・辞書学習を声で。コマンド語彙
  （`parse_talk_command`）はその入口（v2+）。

## 段階的ロードマップ

**v1（実装済み — talk.py）**: 上記モデルの全骨格。意味論的終端検出、エポック・
キャンセル、融合（THINKING中に話し始めたら同一ターンに合流）、VOICEVOX→SAPI→
テキストのTTS梯子、ホットキー割り込み＋headphonesモードの音声割り込み、
「貼って」「終了」、`--text` モード（マイク不要で全ループ検証可）、
`--debug` のターン別レイテンシ計測（gap p50/p95）。

**v2（次）— リズムの完成**:
- 相槌（0円・0msのWAVキャッシュから「うん」「なるほど」— INCOMPLETE の長い間に）
- 思考音（「えっと、」キャッシュ再生でLLMの初token時間を体感150msに隠す）
- 起動時エコー較正ハンドシェイク（既知のTTS音声を再生して結合度を測る）＋
  スピーカーでの pause-then-verify 自己エコー照合（`is_self_echo`）
- talkbench: 会話レイテンシの回帰ベンチ（bench.py 文化の会話版、results.jsonl）
- `--debug` イベントトレースの保存と、純粋 TurnEngine でのオフライン再生
  （ターンテイキングのバグを Ubuntu CI 上で再現する）
- interpreter.py の VAD ループを共通 Segmenter クラスへ抽出（等価性テスト必須）

**v3（その先）**:
- フルデュプレックス既定化（較正済みエコーゲートが実証されたら）
- Interpreter 三者会話プロキシ（仮想マイクへTTS出力）
- トレイ統合（会話モードをホットキーで常駐起動）・表示専用オーバーレイ（D17）
- セッションを跨ぐ記憶（ローカル保存の会話メモリ）

## 採らない道（理由つき）

- **AEC（音響エコーキャンセル）** — 汎用AECより「自分が何をいつ話したか」を
  知っている非対称優位（テキスト照合）が軽くて確実。v2の照合で足りなければ再考。
- **ウェイクワード** — 常駐＋ホットキー＋ハンズフリーVADで足りる。誤起動と
  常時推論のコストに見合わない。
- **固定無音タイムアウト** — 「歩きながら考える人」を必ず遮る。semantic
  endpointing が本プロジェクトの回答（D24）。
- **クラウドTTS/LLM必須の機能** — テーゼ違反（D01）。ローカルの梯子で常に動く。

## 設計パネルの記録（2026-07-01）

この設計は5つの独立提案（会話リアリズム／新用途／最小リスク合成／レイテンシ工学
／後継者継続性）を3視点（オーナー／保守的スタッフエンジニア／会話UX研究者）で
審査して統合した。採用: 純粋ステートマシンを仕様書とする骨格＋意味論的終端検出＋
エポック・キャンセル＋計測文化。会話リアリズム案（相槌・フルデュプレックス）は
「理想への最深の忠実さ、ただしv1の検証不能な表面積が過大」との評でv2/v3へ、
レイテンシ案の投機的STTは「断片の先行文字起こし」として構造ごと吸収した。

---

## English summary

The north star: interacting with AI as a **genuinely sequential conversation**
— turns end on *meaning* (semantic endpointing), the AI is interruptible in
milliseconds (epoch cancellation), replies are spoken and start in ~1s
(sentence-pipelined local TTS), the conversation produces artifacts (「貼って」
pastes the reply via the injector), and all of it runs 100% locally — which is
precisely what makes zero-cost/zero-latency conversational behaviors (aizuchi
backchannels, always-on presence) possible at all. v1 ships the full skeleton
in `talk.py` + `koe/turntaking.py` (the pure, test-pinned spec); v2 completes
the rhythm (backchannels, thinking sounds, echo verification, talkbench); v3
goes full-duplex and turns the Interpreter into a three-way conversation proxy.
Rejected: AEC, wake words, fixed silence timeouts, anything cloud-required.
