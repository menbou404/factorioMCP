# 13. 工場づくりへの拡張計画

## 目的

AI キャラクターが空の持ち物から素材を集め、電力で動く自動化ラインを建て、研究を進められるところまで MCP ツールを広げる。各段階で、決め打ちの手順による統合試験を CI で実行し、ゲーム内の結果で合否を判定する。[共通の不変条件](README.md#共通の不変条件) はすべての段階で守る。

## 段階

| 段階 | 内容 | 統合試験 |
| --- | --- | --- |
| M1 観察の強化 | 設備の稼働状態、向き、入出力位置、ベルト上のアイテム、周囲の資源の集計、設置の事前確認、設備の回収、回転 | `logistics_line.py` |
| M2 物流 | 燃料式インサーター、ベルト、箱による自動搬送。設置できる設備と設備の区画の拡充 | `logistics_line.py` |
| M3 電力 | 汲み上げポンプ、ボイラー、蒸気機関、小型電柱、電動の掘削機・インサーター | M3・M4 の試験 |
| M4 組立と研究 | 組立機のレシピ設定、ラボへの投入、研究の開始、研究可能な技術とレシピの情報 | M3・M4 の試験 |
| M5 総合試験 | 空の持ち物から、電力で動く赤サイエンスの自動化ラインを AI の操作だけで建て、ラボで研究を完了させる | M5 の試験 |

M1 と M2 は 1 つの変更として実装する。M3 以降の詳細は、各段階の着手時にこの文書へ追記する。

## M1・M2 の設計

### 観察結果の追加項目

`observe_nearby` の各エンティティに、該当する場合だけ次の項目を加える。

| 項目 | 対象 | 内容 |
| --- | --- | --- |
| `direction` | 向きを持つエンティティ | `defines.direction` の値（0、4、8、12 など） |
| `status` | 状態を持つエンティティ | `defines.entity_status` の名前（例: `working`、`no_fuel`、`no_power`、`output_full`、`no_input_fluid`、`waiting_for_source_items`） |
| `owned` | すべて | AI キャラクターと同じ勢力なら `true` |
| `bounding_box` | 資源以外 | `{left_top = {x, y}, right_bottom = {x, y}}` |
| `recipe` | 組立機・炉 | 現在のレシピ名。未設定なら省略 |
| `pickup_position` | インサーター | 拾う位置 |
| `drop_position` | インサーター、採掘機 | 置く位置 |
| `belt_items` | ベルト | ベルト上のアイテム。両方の列を合算した `{name, quality, count}` の配列 |

観察結果の最上位に `resources` を加える。観察半径内の資源エンティティを資源名ごとに集計し、`{name, tiles, total_amount, left_top = {x, y}, right_bottom = {x, y}}` を返す。ページングの対象外とし、エンティティ一覧のページに関係なく常に半径内の全資源を集計する。半径外の資源は含めない。

### 新しいツール

| ツール | 種別 | 内容 |
| --- | --- | --- |
| `check_placement(item, surface, x, y, direction)` | 読み取り | `place_entity` と同じ検証を行い、何も消費・生成しない。`{placeable, reason, entity, has_item, bounding_box}` を返す |
| `rotate_entity(target_ref, reverse)` | 変更 | 手の届く自分の勢力の設備を、プレイヤーの回転操作と同じく 90 度回す。新しい `direction` を返す |

`check_placement` の `placeable` は、距離・観察・設置可否（衝突、地形、向き）の検証結果である。持ち物の有無は `has_item` に分けて返し、`placeable` の判定には含めない。`reason` は不可のときだけ `place_entity` と同じエラーコード（`out_of_range`、`not_visible`、`blocked`、`invalid_target`）を入れる。`bounding_box` は向きを反映した設置予定の範囲である。

`rotate_entity` は変更操作として `request_id` の再送防止の対象に加える。回転できない設備は `invalid_target`、手が届かない場合は `out_of_range` を返す。

### 設備の回収

`mine_entity` で、AI キャラクターと同じ勢力の設置済み設備を回収できるようにする。ゲームの通常の採掘と同じく、設備のアイテムと中身がキャラクターの持ち物に戻る。

- 資源は従来どおり `resource_reach_distance`、それ以外は `reach_distance` で距離を判定する。
- 他勢力の設備は `invalid_target` とする。木や岩など勢力を持たない自然物は従来どおり採掘できる。
- 回収の完了は、持ち物の増加と対象の消滅で判定する。`detail` に得たアイテム数を返す。

### 設置できる設備と区画

許可リストに `burner-inserter` を加える。M2 で扱う設備の区画は次のとおり。

| 設備 | 区画 |
| --- | --- |
| `burner-inserter` | `fuel` |
| `wooden-chest`、`iron-chest` | `main`（既存） |
| `stone-furnace` | `source`、`result`、`fuel`（既存） |
| `burner-mining-drill` | `fuel`（既存） |

ベルトの向きは搬送方向とする。インサーターの向きとの対応は Factorio の定義に従い、観察結果の `pickup_position` と `drop_position` で確認できるようにする。

2026-10-23 の実機確認（Factorio 2.0.77）では、燃料式インサーターは `direction` が指す側から拾い、反対側へ置いた。`direction = 12`（西）のとき、西隣のベルトから拾い、東隣の炉へ置いた。

### 統合試験 `logistics_line.py`

`production_line.py` と `negative_checks.py` の後に、同じサーバーで実行する。

1. `production_line.py` で建てた燃料式掘削機と石の炉を `mine_entity` で回収し、設備が消えて持ち物に戻ることを確認する。炉の中の鉄板も持ち物に戻る。
2. `/c` で、原点の西側（x = -12 前後）に新しい鉄鉱石の資源を配置する。素材の配置は `production_line.py` と同じ扱いで、アイテムの付与ではない。
3. 回収した鉄板で歯車、ベルト、燃料式インサーターを手作業製作する。燃料には既存の石炭の資源を採掘して使う。
4. `check_placement` で設置位置を確認してから、掘削機、ベルト 2 本、燃料式インサーター、石の炉を順に置く。ベルトは掘削機の `drop_position` から始め、インサーターの `pickup_position` がベルト上、`drop_position` が炉の上になるよう向きを決める。合わない場合は `rotate_entity` で直す。
5. 掘削機、インサーター、炉に石炭を入れる。以降は鉄鉱石を手で補充しない。
6. 観察で `resources` の集計、ベルト上のアイテム、各設備の `status` を確認する。
7. 炉の `result` に鉄板が 5 枚以上たまったら成功とし、`LOGISTICS_LINE_SUCCEEDED` を出力する。

CI の `integration` ジョブで `negative_checks.py` の後に実行する。

### 受け入れ条件

- 既存の 17 ツールの挙動と応答の既存項目は変えない。項目の追加だけとする。
- `check_placement` は何も消費・生成しない。
- 回収と回転は、持ち物と設備の数がゲームの通常操作と同じだけ変わる。
- `logistics_line.py` が手元の隔離環境と CI の両方で成功する。
- 単体テストで、新しいツールの入力スキーマと、Mod の応答を MCP の応答に変換する処理を確認する。
