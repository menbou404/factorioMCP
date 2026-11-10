# 36. 22 回目の試行で見つかった問題の修正（M28）

## 背景

22 回目の試行（[35-trial21-fixes.md](35-trial21-fixes.md)）で、AI はシード 777 のマップでも、燃料も含めて手運びを禁じた目標を達成した。AI は次の問題を挙げた。

| 区分 | 問題 | 原因 |
| --- | --- | --- |
| BP | 待っている間にベルトで運ばれ、次の操作が `out_of_range` になった | 待つツールの結果に、キャラクターの位置が無い。歩いた先がベルトの上でも知らせない |
| BQ | `place_line` が木や水で途中まで置いて止まった（104 回中 42 回） | 置く前に線全体を確かめる手段が無い。`check_placement` は 1 タイルずつで、手の届く範囲に限る |
| BR | サイエンスの到着の間隔で研究の進みが 10 秒止まり、`wait_for_research` が `stalled` で 6 回中断した | 停滞と見なす秒数が 10 秒に固定されている |
| — | インサーターのフィルターとスプリッターの優先度を設定できない | 新しいツールが要るので、M29 で扱う |

## BP. 待っている間の位置

- `wait_for_items` と `wait_for_research` は、結果に `character_position`（`x`、`y`）を加える。
- 待つ前と後で、キャラクターが 0.5 タイルを超えて動いたときは、`position_drift` に `from`、`to`、`distance` を加え、ベルトに運ばれた可能性があるという `hint` を付ける。もとの `hint` があるときは、両方を残す。
- `move_to` が成功して止まった位置が、`transport-belt` 型、`underground-belt` 型、`splitter` 型の設備の上のときは、結果に `standing_on_belt: true` を加え、ベルトの外に立つよう `hint` を付ける。
- 位置は本人の情報なので、不変条件 5 は変わらない。

## BQ. 線の事前確認 `check_line`

- 新しいツール `check_line(item, surface, from_x, from_y, to_x, to_y)` を加える。引数の条件は `place_line` と同じとする。
- 始点から順に、各タイルに置けるかを調べる。置かない。持ち物も消費しない。持ち物が無くても調べられる。
- 手の届く範囲は調べない。代わりに、各タイルは今の周囲か最近の観察の範囲内である必要がある。範囲外のタイルは `not_visible` とする。観察の範囲の外の地形や木は返さないので、不変条件 5 は変わらない。
- 結果は `requested`、`clear`（置けるタイル数）、`already_present`、`direction`、`complete`（全部置けるか）と、置けないタイルの一覧 `problems` を返す。`problems` は最大 16 件で、各項目は `x`、`y`、`reason`（`blocked`、`not_visible`、`different_direction`）と、`blocked` のときは邪魔な設備の `name` と `target_ref`、または地形の名前を持つ。17 件目以降は数だけ `problem_total` で返す。
- 説明文に、`place_line` の前に使うと木や水を先に見つけられること、木は `mine_entity` で取り除けることを書く。

## BR. 研究の停滞判定

- `wait_for_research` に `stall_seconds` を加える。5 以上 50 以下で、既定は 10 とする。既定の動作は変えない。
- 説明文に、サイエンスが間をあけて届くときは大きくすると書く。

## 効果の確認

M28 のあとの試行も、シード 123 以外のマップで行う。
