# 08. MCP ツールと操作プロトコル

## 初期ツール

| ツール | 種別 | 内容 |
| --- | --- | --- |
| `factorio_ping` | 読み取り | Mod と RCON の接続確認 |
| `get_agent_status` | 読み取り | 生死、位置、体力、現在の操作 |
| `observe_nearby` | 読み取り | 半径 16 タイルの周囲 |
| `get_inventory` | 読み取り | AI キャラクターの主インベントリ |
| `get_entity_inventory` | 読み取り | 手の届く設備の入出力・燃料の中身 |
| `list_craftable_recipes` | 読み取り | 現在の手作業製作候補と製作可能数 |
| `get_research_status` | 読み取り | 所属勢力の研究済み・進行中の技術 |
| `move_to` | 変更 | 目的地へ歩行 |
| `mine_entity` | 変更 | 指定対象を通常の時間をかけて採掘 |
| `craft` | 変更 | 手作業製作をキューに追加 |
| `place_entity` | 変更 | アイテムを消費して設備を設置 |
| `transfer_item` | 変更 | キャラクターと設備の間でアイテムを移す |
| `get_action_status` | 読み取り | 長時間操作の進捗と結果 |
| `cancel_action` | 変更 | 自分の進行中の操作を中断 |
| `get_request_result` | 読み取り | 通信切断後に元の変更要求が実行されたか照会 |

キャラクターの初期生成と復旧は管理機能であり、AI に使わせる通常ツールに含めない。将来の複数化に備えて内部では `agent_id` を全要求に含める。初期版の AI 接続には `agent-1` を固定で割り当て、AI が別の ID を指定して他者を操作できる形にはしない。

## 要求・応答

MCP サーバーは入力を検証し、Mod の独自コマンドに構造化データを渡す。要求には `protocol_version`、一意な `request_id`、`agent_id`、`operation`、`params` を含める。Mod は `request_id`、ゲーム tick、結果、エラーコードを JSON で返す。RCON 応答は呼び出し中に返す必要があるため、時間のかかる操作は操作 ID を返して後から照会する。

周囲観察で見つけた資源や設備には短命の `target_ref` を付ける。採掘・建設・アイテム移動では、Mod が位置、種類、実在、距離を再検証する。参照先が消えた場合は `target_gone` を返し、同じ位置の別物を暗黙に操作しない。

変更操作は `queued`、`running`、`succeeded`、`failed`、`cancelled` の状態を持つ。`succeeded` はゲーム内で結果が確認された状態だけを指す。MCP サーバーは同一キャラクターへの競合する変更操作を直列化し、Mod も 1 体に同時実行できない操作を拒否する。タイムアウトや再送時は同じ `request_id` の結果を照会し、安易に再実行しない。

## エラー

最低限 `not_connected`、`mod_unavailable`、`invalid_input`、`character_dead`、`out_of_range`、`not_visible`、`target_gone`、`missing_materials`、`inventory_full`、`blocked`、`action_conflict`、`timeout` を区別する。エラーには再判断に必要な現在位置、対象の有無、不足アイテムなどを可能な範囲で添える。RCON の生のコマンド結果や秘密情報は返さない。

## 性能と耐障害性

- 観察と状態照会には件数・サイズ上限を設ける。移動や採掘の毎 tick の処理は Mod 内で進め、AI から毎 tick コマンドを送らせない。
- MCP サーバーと RCON の切断時、Mod の進行中操作は停止できる状態に保つ。再接続後は操作 ID で実状態を照合する。
- MCP ツールは自由形式の Lua、RCON コマンド、任意のファイル操作を受け付けない。

参照: [MCP のツール](https://ts.sdk.modelcontextprotocol.io/v2/)、[Factorio の独自コマンド](https://lua-api.factorio.com/latest/classes/LuaCommandProcessor.html)、[RCON 応答](https://lua-api.factorio.com/latest/classes/LuaRCON.html)。
