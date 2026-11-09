# 34. 20 回目の試行で見つかった問題の修正（M26）

## 背景

20 回目の試行（[33-trial19-fixes.md](33-trial19-fixes.md)）で、AI は燃料も含めて手運びを禁じた目標を達成した。一方で、呼び出しは 793 回、入力トークンは 2,432 万で、19 回目の 1.5 倍になった。AI は次の問題を挙げた。

| 区分 | 問題 | 原因 |
| --- | --- | --- |
| BJ | 長いベルトの敷設に呼び出しが多くかかる | ベルト 442 本を 1 本ずつ指定した。`place_entities` は 1 回 10 本までで、結果も 1 本ごとに返す。「歩く→置く」の組が 101 回あった |
| BK | 持ち物の読み取りが、電動ドリルとインサーターを拒否する | `get_entity_inventory` は持ち物のある設備だけに対応する。AI は状態を見たいだけでも `invalid_target` になる |
| BL | ベルトを観察すると結果が大きく、途中で切れる | ベルト 1 本で約 400 バイト返す。`observe_nearby` は全結果データの 26% を占め、100 本で 39 KB になった |
| — | 歩行が機械のそばで止まることがある | 233 回のうち失敗は 1 回で、今回は修正しない |

## BJ. 直線をまとめて置く `place_line`

- 新しいツール `place_line(item, surface, from_x, from_y, to_x, to_y)` を加える。対象は、設置結果の種類が `transport-belt` または `pipe` のアイテムに限る。
- 始点と終点は同じ行か同じ列にあり、2 タイル以上 64 タイル以下とする。それ以外は `invalid_input` とする。ベルトの向きは始点から終点へ進む向きとする。
- 始点から順に、1 タイルずつ `place_entity` と同じ検査で置く。手の届く範囲、観察の条件、持ち物の消費は変えない。歩かない。
- 最初に置けなかったタイルで止まり、それ以降は試さない。すでに置いた分は残す。
- 同じ名前で同じ向きの設備が既にあるタイルは、置かずに `already_present` に数えて先へ進む。パイプは向きを比べない。同じ名前で向きが違うときは、`blocked` で止まり、`detail.reason = "different_direction"` と `existing_direction` を返す。
- 結果は 1 本ごとの一覧を返さない。`requested`、`placed`、`already_present`、`complete`、`direction`、`first_position`、`last_position`、`next_position`、`stopped`（止まった理由のエラーコード）、`detail`、`hint` を返す。`next_position` は最初に置けなかったタイルである。
- 呼び出し自体が実行されたときは `ok: true` とする。全部置けたかは `complete` で判断する。歩行中や採掘中は `action_conflict` とする。
- 説明文に次を書く。手の届く距離は 10 タイルなので、線の中央付近に立てば 1 回で約 21 タイル置ける。止まったら `next_position` の近くへ歩いて、そこを始点に呼び直す。角を曲がるときは、前の線の最後のベルトが向いているタイルを次の線の始点にする。

## BK. 持ち物のない設備の状態

- `get_entity_inventory` と `get_entity_inventories` は、自勢力の `inserter` 型の設備と `mining-drill` 型の設備のうち、持ち物の役割が定義されていないものでも、手の届く範囲なら成功を返す。`inventories` は空にする。
- `status` に加えて、`inserter` 型は `pickup_position`、`drop_position`、`held_stack`（掴んでいる物。無ければ省く）を返す。`mining-drill` 型は `drop_position` と、掘っている資源の名前 `mining_target` を返す。
- それ以外の未対応の設備は、これまでどおり `invalid_target` とする。距離と所属勢力の検査も変えない。

## BL. `observe_nearby` の省略表示

- `observe_nearby` に `compact` を加える。既定は `false` で、今までの結果を変えない。
- `compact = true` のとき、設備の項目は `target_ref`、`name`、`x`、`y` と、値があれば `direction`、`status`、`recipe`、`belt_items`、`underground_type`、`neighbour`、`pickup_position`、`drop_position`、`electric_network_id` に絞る。`type`、`distance`、`surface`、`unit_number`、`owned`、`bounding_box` などは省く。`entity_total` やページ送りは変えない。

## 効果の確認

21 回目の試行（2026-11-09、シード 123、敵なし）で、燃料も手で入れない条件を守って `military-2` の研究まで完了した。この条件での成功は 20 回目と合わせて 2 回で、どちらも同じシードである。

| 項目 | 20 回目 | 21 回目 |
| --- | --- | --- |
| ツール呼び出し | 793 回 | 784 回 |
| エラー | 27 回 | 37 回 |
| 入力トークン | 2,432 万 | 1,886 万 |
| かかった時間 | 68 分 | 61 分 |
| 設置したベルト | 442 本 | 294 本 |
| 「歩く→置く」の組 | 101 回 | 36 回 |
| 観察の 1 設備あたりの大きさ | 約 390 バイト | 約 123 バイト |
| 最後の研究 | `automation-2`（赤緑 40 個ずつ） | `military-2`（赤緑 20 個ずつ） |

- 入力トークンは 22% 減った。ただし、21 回目はベルトが少なく、最後の研究も軽いので、減少のすべてが BJ から BL の効果とは言えない。
- `place_line` は 53 回呼ばれ、ベルト 284 本とパイプ 107 本を置いた。`observe_nearby` は 50 回すべてで `compact` が使われた。設備 1 つは小さくなったが、観察の回数が増えて、観察の結果データの合計は 15 万バイトから 28 万バイトに増えた。
- エラーのうち 16 回は、`move_to` が `running` を返したあとに AI が続きの操作をまとめて送ったもので、ツールの不具合ではない。
- 手で入れた最後の品は、研究の完了直前である 681 手目の赤サイエンスである。AI は `military-2` の消費が始まる前に、研究所に残った赤サイエンスを取り出した。710 手目の研究所には緑サイエンスだけが入っていた。終了時の RCON の確認では、緑サイエンスの消費は 20 個で、組立機の生産は 50 個である。
- 残った指摘は [35-trial21-fixes.md](35-trial21-fixes.md) で扱う。
