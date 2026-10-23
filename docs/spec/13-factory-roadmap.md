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

## M3・M4 の設計

M3（電力）と M4（組立と研究）は 1 つの変更として実装する。M4 までで「蒸気発電でラボを動かし、手作業で作った赤サイエンスで研究を 1 つ完了する」ところまでを統合試験で確かめる。組立機による赤サイエンスの自動生産は M5 で扱う。

### 設置できる設備と区画

許可リストに `offshore-pump`、`boiler`、`steam-engine`、`lab` を加える。`small-electric-pole`、`inserter`、`electric-mining-drill`、`assembling-machine-1`、`pipe` は既に許可済みである。電柱はゲームの既定どおり、近くの電柱に自動で接続する。

| 設備 | 区画 |
| --- | --- |
| `boiler` | `fuel` |
| `lab` | `input` |
| `assembling-machine-1` | `source`、`result`（既存） |

### 観察結果の追加項目

| 項目 | 対象 | 内容 |
| --- | --- | --- |
| `fluids` | 流体の区画を持つ設備 | 各区画の流体 `{name, amount}` の配列。空の区画は省略する |
| `electric_network_id` | 電気で動く設備と電柱 | 接続している電力網の ID。未接続なら省略する |

稼働状態の `status`（`no_power`、`low_power`、`no_input_fluid` など）と合わせ、AI が電力と流体のつながりを判断できるようにする。

### 新しいツール

| ツール | 種別 | 内容 |
| --- | --- | --- |
| `set_recipe(target_ref, recipe)` | 変更 | 手の届く自分の組立機にレシピを設定する |
| `start_research(technology)` | 変更 | 研究を研究キューに加える（キューが無効なら即座に開始） |
| `get_recipe_info(recipe)` | 読み取り | レシピの有効・無効、カテゴリ、所要時間、材料、生成物、手作業で作れるか |

`set_recipe` の規則:

- 対象は自分の勢力の組立機で、手が届くこと。レシピは勢力で有効になっており、その組立機で作れるカテゴリであること。満たさない場合は `invalid_target`、`recipe_unavailable` を返す。
- 組立機の入力・出力に中身がある場合は `not_empty` を返し、レシピを変えない。レシピ変更でアイテムが外に出て消えたり増えたりする経路を作らない。
- `request_id` による再送防止の対象にする。

`start_research` の規則:

- 技術が存在し、勢力で有効で、未研究で、前提技術がすべて研究済みであること。満たさない場合は `invalid_target` を返す。
- ゲームの `LuaForce.add_research` を使う。研究の進行はラボとサイエンスパックによってのみ進み、MCP から研究を完了させる経路は作らない。
- `request_id` による再送防止の対象にする。

`get_research_status` に、研究キュー `queue` と、今すぐ研究できる技術の一覧 `available`（`{name, unit_count, unit_time, ingredients}`、名前順、最大 100 件）を加える。

### 製作トリガー型の技術

Factorio 2.0 では、序盤の技術の一部がラボを使わず、特定のアイテムを製作したときに研究済みになる（`research_trigger` の `craft-item`）。例えば `automation-science-pack` はラボの製作で解禁され、赤サイエンスのレシピが有効になる。

2026-10-23 の実機確認で、炉が作った鉄板・銅板による解禁（`steam-power`、`electronics`）は働いたが、AI キャラクターの手作業で作ったラボでは `automation-science-pack` が解禁されなかった。AI キャラクターはプレイヤーに紐付かないため、ゲーム本体が手作業の製作をトリガーとして数えないためである。通常のプレイヤーなら解禁される条件なので、Mod が次の範囲だけ補う。

- AI キャラクターの手作業の製作操作が `succeeded` で完了したとき、そのレシピの生成物の数を、アイテムごとの累計として保存する。
- 累計が技術の `research_trigger`（`craft-item`）のアイテムと数量を満たし、技術が有効・未研究で、前提技術がすべて研究済みなら、その技術を研究済みにする。
- 中断・失敗した製作は数えない。`craft-item` 以外のトリガーは対象外とし、必要になった段階で同じ方針で追加を検討する。
- この処理は MCP のツールではなく、製作の完了時に Mod の内部で行う。MCP から任意の技術を完了させる経路は作らない。

`get_research_status` の `available` には、トリガー型の技術の条件 `trigger`（`{type, item, count}`）も含め、AI が解禁の方法を判断できるようにする。

### 発電の配置例

Factorio 2.0.77 のプロトタイプ定義（`data/base/prototypes/entity/entities.lua`）から求めた、配管なしで直結できる配置である。統合試験ではこの配置を使い、設置前に `check_placement` で確認する。

- 湖: x = 10〜13、y = -22〜-17 のタイルを水にする。
- 汲み上げポンプ: (14.5, -19.5)、`direction = 12`（西）。西側の水を汲み、東へ送り出す。
- ボイラー: (16.5, -20)、`direction = 0`。西端の水の口がポンプとつながり、北側から蒸気を出す。
- 蒸気機関: (16.5, -23.5)、`direction = 0`（縦向き）。南端の口がボイラーの蒸気の口とつながる。

電柱は蒸気機関の近くから、電線が届く 7.5 タイル以内の間隔でラボまでつなぐ。

### 統合試験 `power_research.py`

`logistics_line.py` の後に、同じサーバーで実行する。すべてのアイテムは採掘・製錬・製作・回収で得る。

1. `logistics_line.py` で建てた設備（掘削機、ベルト、インサーター、炉）を回収する。
2. `/c` で、資源（鉄鉱石・銅鉱石は 4 × 4 タイル、石と石炭は 2 × 2 タイル、いずれも十分な埋蔵量）と湖を配置する。地形と資源の配置であり、アイテムの付与ではない。
3. 燃料式掘削機の出力先に石の炉を置く組を、鉄鉱石と銅鉱石の上に作る。製錬した鉄板で掘削機と炉を増やし、必要な鉄板と銅板がたまるまで燃料の補充と回収を繰り返す。石と石炭は手で採掘する。
4. 必要な数は `get_recipe_info` と `list_craftable_recipes` から計算する。決め打ちの数を使う場合も、材料不足なら明確なメッセージで失敗させる。
5. 発電の配置例どおりにポンプ、ボイラー、蒸気機関を置き、ボイラーに石炭を入れる。電柱でラボまでつなぎ、観察の `status` と `fluids` で発電を確認する。
6. 赤サイエンス（`automation-science-pack`）を 10 個手作業で作ってラボに入れ、`start_research("automation")` を呼ぶ。
7. `get_research_status` の `researched` に `automation` が入ったら成功とし、`POWER_RESEARCH_SUCCEEDED` を出力する。

CI の `integration` ジョブで `logistics_line.py` の後に実行する。所要時間が延びるため、ジョブの時間制限を見直す。

### 受け入れ条件

- 既存のツールと応答の既存項目は変えない。
- `set_recipe` と `start_research` は、条件を満たさないときにゲーム状態を変えない。
- 研究はラボでのサイエンスパックの消費によってのみ進む。
- `power_research.py` が手元の隔離環境と CI の両方で成功する。
