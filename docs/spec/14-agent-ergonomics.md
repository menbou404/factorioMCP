# 14. AI エージェント向けの使いやすさ改善（M6）

## 背景

2026-11-02 に、Codex（`gpt-6.1-sol`）を MCP クライアントとして、通常生成のマップ（シード 123、平和モード）で自律的に遊ばせた。目標（組立機が作った赤サイエンスで研究を 1 つ完了する）は 19 分で達成した。ツール呼び出しは 480 回で、内訳と問題は次のとおりだった。

| 観察された問題 | 根拠 |
| --- | --- |
| 観察の回数が多い | `observe_nearby` が 136 回（28%）。資源が密集した場所で、設備を探すために何百件もページをめくった |
| 完了待ちの問い合わせが多い | `get_action_status` が 112 回（23%） |
| エラーから次の手が分からない | 区画名の誤り（`input` と `source`）、設置を妨げる物、観察の期限切れで、エラーコードだけが返った |

1 回の試行で Codex の 5 時間枠の 25% を使った。M6 は、AI が同じ目標を少ない呼び出しで達成できるようにする。共通の不変条件は変えない。

## 観察の絞り込み

`observe_nearby` に次の任意の引数を加える。省略時の挙動は従来どおりとする。

| 引数 | 型 | 内容 |
| --- | --- | --- |
| `radius` | 1〜16 の数 | 観察半径。既定は 16 |
| `types` | 文字列の配列 | エンティティの種類（`type`）で絞る |
| `names` | 文字列の配列 | エンティティ名で絞る |
| `owned_only` | 真偽値 | 自分の勢力のものだけにする |
| `include_resources` | 真偽値 | 既定は `true`。`false` なら資源エンティティを一覧から外す（`resources` の集計は残す） |
| `include_tiles` | 真偽値 | 既定は `true`。`false` なら通行できないタイルの一覧を返さない |

- 絞り込みはページングの前に行う。`entity_total` は絞り込み後の件数とする。
- `resources` の集計は、絞り込みに関係なく観察半径内のすべての資源から作る。
- 設置で使う「直近の観察」は、実際に観察した中心・半径・時刻を記録し、`place_entity` と `check_placement` はその半径で可視性を判定する。絞り込みは見えた範囲を変えないので、可視性の判定には使わない。
- 応答に、適用した絞り込みを `filters` として返す。

## 完了まで待つ

`wait_for_action(action_id, timeout_seconds)` を MCP サーバーに加える。

- MCP サーバーの中で、0.25 秒ごとに `get_action_status` を呼び、`running` でなくなるか時間切れになったら、最後の状態を返す。Mod の変更は要らない。
- `timeout_seconds` は 1〜30、既定は 20。時間切れの場合は `timed_out = true` を付け、状態は `running` のまま返す。
- 待っている間も、既存の接続確認（ハートビート）は続く。

## エラーへの案内

Mod は、エラーの理由が分かる場合に `detail` を応答に含める。MCP サーバーは `detail` をそのまま返し、エラーコードごとの短い案内 `hint` を加える。既存のエラーコードは変えない。

| エラー | `detail` の内容 |
| --- | --- |
| `invalid_target`（区画） | `entity`、`valid_roles`（その設備で使える区画名） |
| `invalid_target`（設備） | `entity`、`reason`（`unsupported_entity`、`other_force`、`not_rotatable` など） |
| `blocked`（設置） | `blocking`（設置範囲に重なるエンティティの `name`・`type`・位置、最大 10 件）、`tiles`（設置できない地形タイル名） |
| `out_of_range` | `distance`、`reach` |
| `not_visible` | `observed`（直近の観察の中心・半径・経過秒数。観察が無ければ省略） |
| `target_gone` | `reason`（`expired` または `entity_gone`） |
| `action_conflict` | `running_action`（`id`、`type`） |
| `missing_materials` | 製作では不足する材料と数、設置では不足するアイテム |

`hint` の例:

- `target_gone`: `target_ref` は観察から 10 秒で失効する。`observe_nearby` で取り直す。
- `not_visible`: 設置位置を含む範囲を 10 秒以内に観察してから設置する。
- `action_conflict`: 実行中の操作を `wait_for_action` で待つか、`cancel_action` で止める。
- `blocked`: `blocking` のエンティティを回収・採掘するか、別の位置を `check_placement` で探す。

`detail` には秘密情報や RCON の生の出力を含めない。

## 試験

- 単体テスト: `wait_for_action` の完了・時間切れ、`detail` と `hint` の受け渡し、観察の新しい引数のスキーマ。
- 統合試験: `negative_checks.py` に、`valid_roles`、`blocking`、`distance`/`reach`、`target_gone` の `reason` を確かめる項目を加える。観察の絞り込み（`types`、`owned_only`、`include_resources=false`、`radius`）と `wait_for_action` を確かめる項目も加える。
- 効果の確認: マージ後に 2026-11-02 と同じ条件（シード 123、同じ指示）で Codex の試行を繰り返し、呼び出し回数・Codex の使用量・所要時間を比べて、この文書に結果を追記する。
