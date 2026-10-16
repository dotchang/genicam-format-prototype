# GCR (GenICam Capture Recipe) 仕様 v1.0

GenICam カメラの撮影条件と撮影データを、ベンダー・機種に依存せず共有するためのファイル形式。
機械検証用の JSON Schema は [`schema/`](schema/) にあり、参照実装 `gcr` の pydantic モデル
(`src/gcr/models.py`) から生成されている (`gcr schema -o spec/schema`)。

> English summary: GCR describes *what* camera settings an experiment needs using SFNC feature
> names (Config), *how* to acquire (Recipe), *how a given camera model deviates* from SFNC
> (Profile), and *what actually happened* during a capture (Bundle: frames + applied settings +
> full read-back snapshot + device info + SHA-256 manifest).

## 1. 設計原則

1. **共通語彙は SFNC**: ノード名は GenICam SFNC (Standard Features Naming Convention) の名前を使う。
   SFNC にない機種固有ノードはプロファイルで別名を与える。
2. **順序は意味を持つ**: 設定は上から順に適用する (例: `ExposureAuto: Off` → `ExposureTime`、
   `PixelFormat`/`Width` → `OffsetX`、Selector → 選択される値)。
3. **意図と実測の分離**: 共有するのは「やりたい設定」(Config/Recipe)。撮影時に実際に読み戻した値は
   Bundle の `applied.json` / `snapshot.json` に記録する。
4. **部分再現を許す**: 各設定に必須度 (`level`) と許容誤差 (`tolerance`) を持たせ、別機種でどこまで
   再現できたかを機械的に判定・記録する。
5. **人が書くものは YAML、機械出力は JSON**。全文書に `gcr_version` と `kind` を持つ。

## 2. 共通フィールド

| フィールド | 型 | 説明 |
|---|---|---|
| `gcr_version` | string | `"MAJOR.MINOR"`。MAJOR が異なる文書は読み込みエラー。現在 `"1.0"`。 |
| `kind` | string | `profile` / `config` / `recipe` / `applied` / `snapshot` / `bundle` |

未知のフィールドはエラー (タイプミス検出のため)。

## 3. Config (`*.config.yaml`)

| フィールド | 説明 |
|---|---|
| `name` | 識別名 |
| `description` | 任意 |
| `target` | 機種名ではなく要件で書く: `pixel_format_family` (`mono`/`color`/`bayer`/`any`), `min_bit_depth`, `min_width`, `min_height`, `notes` |
| `settings` | 下記 Setting の配列。**記述順に適用** |

### Setting

| フィールド | 既定 | 説明 |
|---|---|---|
| `node` | – | SFNC 名 (または機種固有名) |
| `value` | – | 目標値。スカラー、または **候補リスト** (先頭から順に、デバイスで使えるものを採用)。数値ノードでは特殊値 `"@max"` `"@min"` `"@center"` (= min と max の中央。`OffsetX` の max は `WidthMax − Width` なのでセンタリングになる) |
| `execute` | `false` | `true` ならコマンドノードを実行 (`value` と排他) |
| `unit` | – | `value` の単位。`s ms us ns Hz kHz MHz dB` 間は自動換算。デバイスが単位を宣言しない場合は SFNC 既定単位 (`ExposureTime`=us, `AcquisitionFrameRate`=Hz, `Gain`=dB) を仮定 |
| `selector` | `{}` | このノードの前に (記述順に) 設定するセレクタ。例 `{GainSelector: All}` |
| `level` | `required` | 再現できなかったときの扱い (下表) |
| `tolerance` | – | 読み戻し値の許容誤差。数値=絶対値 (`unit` 単位)、文字列 `"1%"`=相対。省略時: 整数は 1 inc 未満、浮動小数は 0.1% |
| `note` | – | 人向けメモ |

設定の識別キーは `Node[Selector=Value,...]` (例 `Gain[GainSelector=All]`)。

### level と適用結果 (status)

| 状況 | `required` | `preferred` | `optional` |
|---|---|---|---|
| ノードが無い / 書けない / 候補が全滅 | `failed` | `skipped` | `skipped` |
| 書けたが読み戻しが許容誤差外 (クランプ等) | `failed` | `approx` | `approx` |
| 許容誤差内 | `ok` | `ok` | `ok` |

`required` に `failed` が 1 つでもあれば適用は失敗 (撮影は中止。`--force` で続行可)。
読み取り専用ノードでも、現在値が目標を満たしていれば `ok`。

## 4. Recipe (`*.recipe.yaml`)

| フィールド | 説明 |
|---|---|
| `name`, `description` | |
| `config` | Config ファイルへの相対パス、またはインライン Config |
| `acquisition.trigger.mode` | `free_run` / `software` / `hardware` (`source` 既定 `Line1`, `activation` 既定 `RisingEdge`) |
| `acquisition.frames` | 保存枚数 |
| `acquisition.frame_rate` | Hz (free_run 時) |
| `acquisition.warmup_frames` | 取得して捨てる枚数 |
| `acquisition.timeout_ms` | 1 枚あたりのタイムアウト |
| `chunks` | 1 枚ごとに記録する ChunkSelector 名 (例 `Timestamp`, `ExposureTime`) |
| `output.format` | `tiff` / `npy` |
| `output.layout` | 拡張子なしのパステンプレート (既定 `frames/{index:06d}`) |

Recipe の `acquisition` と `chunks` は、Config の後に適用される暗黙の Setting に展開される:

| 条件 | 展開される Setting |
|---|---|
| 常に | `AcquisitionMode=Continuous` (optional) |
| free_run | `TriggerMode[TriggerSelector=FrameStart]=Off` (preferred); `frame_rate` があれば `AcquisitionFrameRateEnable=true` (optional), `AcquisitionFrameRate` (preferred) |
| software / hardware | `TriggerMode[...]=On`, `TriggerSource[...]=Software`/`source` (required); hardware は `TriggerActivation` (preferred) |
| chunks あり | `ChunkModeActive=true` (required), `ChunkEnable[ChunkSelector=X]=true` (preferred) |

## 5. Profile (`*.profile.yaml`)

| フィールド | 説明 |
|---|---|
| `name` | |
| `match` | `vendor` / `model` / `device_version` の fnmatch パターン (大文字小文字無視)。複数一致時はワイルドカード以外の文字数が多いものを優先 |
| `verified` | 実機で確認済みか |
| `aliases` | `SFNC名: 機種ノード名` |
| `conversions` | `SFNC名: {node, scale, offset, unit}`。`SFNC値 = raw × scale + offset` |
| `quirks` | 人向けの注意事項 |

解決順: conversions → aliases → 同名ノード。プロファイルは `--profile` で明示するか、
`--profiles-dir`、環境変数 `GCR_PROFILE_PATH`、カレントの `profiles/` から自動選択される。

## 6. Capture Bundle

```
<bundle>/
  manifest.json      kind: bundle。Config をインライン化した recipe、その SHA-256、summary、全ファイルの SHA-256
  applied.json       kind: applied。各 Setting の requested / applied / readback / status / message
  snapshot.json      kind: snapshot。撮影直前の全 readable feature (セレクタ展開済み、キー Node[Sel=Val])
  device.json        GenTL のデバイス情報、Device* ノード値、Producer (.cti 名と SHA-256)、ホスト/ライブラリ版
  genicam/<file>     デバイスから読み出した GenICam XML (または zip) 原本
  frames/000000.tiff ...
  frames.csv         index, file, frame_id, device_timestamp_ns/ticks, host_timestamp_ns, pixel_format, incomplete, chunk_*
```

- `manifest.recipe_sha256` = recipe を `sort_keys` 付き JSON 化した UTF-8 バイト列の SHA-256。
- `manifest.files` は `manifest.json` 以外の全ファイルを列挙する。`gcr verify` は欠落・改変・未登録ファイルを検出する。
- Bundle を zip にしてもよい (ルート直下か 1 階層下に `manifest.json`)。
- `summary.missing_frame_ids` は保存フレームの `frame_id` の欠番。

## 7. 互換性ポリシー

- MINOR 版の追加はフィールド追加のみ (古いリーダーはエラーになるため、ツールは同 MAJOR の新しい MINOR を受理するよう更新すること)。
- MAJOR 版は互換性のない変更。

## 8. 既知の制限 (v1.0)

- マルチコンポーネント (GenDC / 3D) は先頭コンポーネントのみ保存。
- 単位換算は時間・周波数・dB のみ。
- `@center` は min/max の中央であり、オフセット以外のノードでは意味が異なる。
- Selector 展開は enum セレクタのみ、1 段のみ。
