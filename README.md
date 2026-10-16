# GCR: GenICam Capture Recipe (prototype)

GenICam カメラの **設定・撮影手順・撮影データ** を、ベンダーや機種に依存せず共有するためのファイル形式と、
Python ([Harvesters](https://github.com/genicam/harvesters)) による参照実装です。

*A vendor-neutral, human-readable and machine-verifiable format for sharing GenICam camera
settings, acquisition procedures and captured data, with a Python/Harvesters reference tool.*

- 仕様: [spec/SPEC.md](spec/SPEC.md) / JSON Schema: [spec/schema/](spec/schema/)
- 設計レポート: [docs/report.html](docs/report.html)

## なぜ

ベンダーツールの設定ファイル (.pfs など) や GenApi の永続化ファイルは機種に紐づいていて、
論文や OSS で「同じ条件で撮る」「このデータはどの条件で撮ったか」を第三者に渡せません。
GCR は SFNC のノード名を共通語彙にし、機種差はプロファイルで吸収し、
撮影時に **実際に設定できた値** を記録することで、異機種間でも再現性を説明できるようにします。

## 4 つの文書

| kind | ファイル | 役割 |
|---|---|---|
| `config` | `*.config.yaml` | カメラ設定 (順序付き、必須度・許容誤差つき) |
| `recipe` | `*.recipe.yaml` | 撮影手順 (トリガ・枚数・チャンク・保存形式) |
| `profile` | `*.profile.yaml` | 機種プロファイル (SFNC 名 → 機種固有ノード、単位換算) |
| `bundle` | ディレクトリ | 撮影データ + `applied.json` + `snapshot.json` + `device.json` + GenICam XML + SHA-256 manifest |

```yaml
# examples/low-noise-mono.config.yaml (抜粋)
settings:
  - { node: PixelFormat, value: [Mono12, Mono12p, Mono16] }   # 候補リスト
  - { node: Width, value: "@max" }
  - { node: OffsetX, value: "@center", level: preferred }
  - { node: ExposureAuto, value: "Off" }
  - { node: ExposureTime, value: 10, unit: ms, tolerance: 1% }
  - { node: Gain, selector: { GainSelector: All }, value: 0.0, unit: dB, level: preferred }
```

## インストール

```bash
python -m venv .venv
.venv/Scripts/activate            # Linux/macOS: source .venv/bin/activate
pip install -e ".[harvesters,dev]"
```

GenTL Producer (.cti) は `--cti` で渡すか、環境変数 `GENICAM_GENTL64_PATH` に置き場所を設定します。
カメラが無くても、`genicam` パッケージ同梱のシミュレータ `site-packages/genicam/viky.cti` で全機能を試せます。

## 使い方

```bash
gcr list    --cti path/to/producer.cti
gcr dump    --cti ... -o mycam.config.yaml [--all]          # 今のカメラ設定を共有用 config に
gcr validate examples/*.yaml
gcr apply   --cti ... examples/low-noise-mono.config.yaml   # 1 行ずつ OK / ~ / SKIP / FAIL を表示
gcr apply   --cti ... my.config.yaml --dry-run               # カメラを変更せずに可否判定
gcr capture --cti ... examples/low-noise-mono.recipe.yaml -o capture_001
gcr verify  capture_001                                       # ハッシュ検証
gcr diff    examples/low-noise-mono.recipe.yaml capture_001 --common   # 意図と実測の差
gcr schema  -o spec/schema
```

シミュレータでの実行例 (`examples/bundle-viky/` はこのコマンドの出力):

```text
$ gcr capture --cti .venv/Lib/site-packages/genicam/viky.cti examples/viky.recipe.yaml --frames 3 -o examples/bundle-viky
camera: Machine Vision Games Ltd. Viky (serial 0); profile: viky-simulator
wrote examples/bundle-viky: {'frames_requested': 3, 'frames_saved': 3, ..., 'apply_ok': True, 'apply_counts': {'ok': 8, 'skipped': 4}}
```

Viky には ExposureTime などが無いため、それらは `optional` として `skipped` と記録され、撮影は継続します。

## テスト

```bash
pytest            # 単体テスト + Viky シミュレータでの dump → apply → capture → verify → diff
```

## 構成

```
spec/SPEC.md, spec/schema/   仕様と JSON Schema (models.py から生成)
src/gcr/                     models, loader, profile, nodemap, apply, snapshot, capture, bundle, cli
profiles/                    機種プロファイル (viky: 検証済み / basler-ace-gige-legacy: 未検証の例)
examples/                    config / recipe / 生成済み bundle
tests/                       pytest
docs/report.html             設計レポート
```

## ステータス

プロトタイプ (v0.1.0, フォーマット 1.0)。シミュレータでのみ検証済みで、実機プロファイルは募集中です。
既知の制限は [SPEC.md §8](spec/SPEC.md) を参照。
