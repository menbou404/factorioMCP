# Factorio AI MCP

Factorio 2.0.77（Space Age 有効）内に AI 専用の独立したキャラクターを 1 体作り、MCP クライアントから操作する試作版です。AI は画面内を歩き、通常の採掘・手作業製作を行います。建設は対応するアイテムを本人の持ち物から消費し、設備とのアイテム移動も実際のインベントリを使います。

設計と制約は [仕様書](docs/spec/README.md) に分けて記載しています。

## 構成

- `mod/factorio_ai_mcp_0.1.0`: Factorio 側の Mod
- `src/factorio_mcp`: RCON と MCP をつなぐ Python サーバー
- `scripts`: この PC 用の起動スクリプト

RCON は Factorio を遠隔操作する管理用通信です。このプロジェクトでは同じ PC の `127.0.0.1:27015` だけで受け付け、MCP サーバーが `/ai_mcp` コマンドへ要求を渡します。マルチプレイ参加用のパスワードとは別に、起動スクリプトが専用のランダムな RCON パスワードを作ります。

## 起動

1. [uv](https://docs.astral.sh/uv/getting-started/installation/) を用意し、このフォルダーで `uv sync` を実行します。Python 3.12 と依存パッケージが `.venv` に準備されます。現在の作業環境では導入済みです。
2. Factorio が起動していない状態で、PowerShell から `./scripts/start-factorio.ps1` を実行します。ゲーム画面が開き、プロジェクトの Mod とローカル RCON が有効になります。初回だけ `.rcon-password` が作成されます。起動スクリプトは画面付き主催用の RCON 設定を含む `.factorio-mcp-config.ini` も生成します。両ファイルは共有しないでください。
3. Factorio の画面で **マルチプレイ → ゲームを主催 → セーブしたゲームを主催** を選び、`NewGameForMCP` を開きます。Mod の差分が表示された場合は、画面上で **Factorio AI MCP Bridge** と表示される Mod（内部 ID `factorio_ai_mcp`）を有効にした状態で読み込みます。マルチプレイ参加用のパスワードは従来の設定を使えます。
4. MCP クライアントに標準入出力型サーバーとして、コマンド `powershell.exe`、引数を **3 つに分けて** `-NoProfile`、`-File`、`E:\factorioMCP\scripts\start-mcp.ps1` と登録します。クライアントから `factorio_ping` と `get_agent_status` を呼び、接続とキャラクター生成を確認します。

Factorio の起動スクリプトを使った場合、Mod はこのプロジェクトの `mod` フォルダーから読み込まれます。Factorio 本体の Mod フォルダーを直接編集する必要はありません。ゲームを閉じている間は MCP の操作は `not_connected` になります。

Python 側の単体テストは `uv run python -m unittest discover -s test -p 'test_*.py'` で実行できます。

## 最初の操作

1. `get_agent_status` でキャラクターの現在位置を確認します。初期持ち物は空です。
2. `observe_nearby` で周囲を読み、資源の `target_ref` を取得します。
3. `move_to` で資源に歩いて近づき、返された `action_id` を `get_action_status` で確認します。
4. `mine_entity` で採掘し、`get_inventory` で取得物を確認します。
5. `list_craftable_recipes` から既知のレシピ、材料、現在製作できる数量を見ます。結果が多い場合は `next_offset` を次の `offset` に指定します。
6. `craft` で手作業製作を始め、`get_action_status` で完了を確認します。`place_entity` は対応する設備アイテムを 1 個消費します。
7. `get_entity_inventory` と `transfer_item` で、近くの箱・石の炉などとアイテムを移動します。

設備や資源の `target_ref` は観察から 10 秒間有効です。期限切れのときは再観察します。作成した設備は観察者の画面にも現れます。

変更操作で `not_connected` が返り、結果に `request_id` が含まれる場合は、接続回復後に `get_request_result` で実行済みかを確認します。同じ操作をすぐに再実行しないでください。

MCP サーバーはゲームへ定期的に接続確認を送ります。接続が約 10 秒途絶えると、進行中の歩行・採掘・製作を停止します。

## 対応範囲と検証

MCP ツールは周囲・持ち物・手作業レシピ・研究状態の読み取り、歩行、採掘、手作業製作、基本設備の設置、箱・石の炉などとのアイテム移動に対応します。設置可能なアイテムは Mod 内の許可リストに限定しています。複数 AI、Space Age 固有の設備、宇宙空間での操作は今後の拡張範囲です。

Python 版で、隔離した検証セーブを使って次を確認しました。素材のみを配置した検証用マップで、空の持ち物から AI キャラクターの操作だけで石・木・石炭・鉄鉱石を集め、炉と燃料式掘削機を作りました。掘削機から炉へ自動供給して鉄板 10 枚を生産できました（[正常系の統合試験](test/manual/production_line.py)）。異常系の確認では次の結果になりました（[異常系の統合試験](test/manual/negative_checks.py)）。

- 材料不足の製作、持っていない物の設置、設備があるマスへの設置は失敗し、持ち物と設備数は変わりません。
- 同じ `request_id` の変更要求を再送しても、二重には実行されません。
- 手の届かない対象、存在しない `target_ref`、期限切れの `target_ref` への操作は失敗します。

`NewGameForMCP` のコピーでも Mod の読み込み、AI キャラクターの生成、セーブ再読み込み後の同一キャラクター維持を確認しました。画面付きマルチプレイ主催での MCP 接続と生存中の AI キャラクターの状態取得を確認しました。人間の画面で AI の歩行を観察する条件は未確認です。

### 統合試験の実行

統合試験はゲームを変更し、`/c` で素材を生成します。実際のセーブには使わず、使い捨てのセーブを読み込んだヘッドレスサーバーで実行してください。

`production_line.py` は、素材を配置する前に原点を中心とした 60 × 60 マスを整地します。キャラクター以外のエンティティを取り除き、地面を草地にして、地表を平和モードにし、既存の敵ユニットを消します。

1. `.factorio-test/rcon-secret.txt` に試験用の RCON パスワードを置きます。
2. 検証用セーブのコピーを `--start-server` で読み込み、`--mod-directory E:\factorioMCP\mod --rcon-bind 127.0.0.1:27277 --rcon-password <上記パスワード>` を付けて Factorio を起動します。`--config` には、書き込み先を `.factorio-test` に向けた設定ファイルを指定します。
3. `uv run python test/manual/production_line.py` を実行し、`PRODUCTION_LINE_SUCCEEDED` が出ることを確認します。
4. 同じサーバーで続けて `uv run python test/manual/negative_checks.py` を実行し、`NEGATIVE_CHECKS_SUCCEEDED` が出ることを確認します。

### CI

CI は PR と main への push、および手動実行で動きます。各ジョブで次を確認します。

- `lint`: Python ファイルの Ruff による静的解析とフォーマットを確認します。手動の統合試験スクリプトも対象です。
- `test (ubuntu-latest)` / `test (windows-latest)`: 単体テストと Mod パッケージの構成・メタデータの検証を実行します。
- `mod`: `mod/` 内のすべての Lua ファイルを Lua 5.2 で構文チェックします。
- `powershell`: `scripts/*.ps1` を Windows PowerShell のパーサーで構文チェックします。スクリプト自体は実行しません。
- `integration`: Linux のヘッドレス Factorio で、整地から生産ラインの構築までの正常系と異常系の統合試験を実行します。所要時間は約 3 分です。現在は必須チェックに含めていません。

Python の検証は、ローカルでも次のコマンドで再現できます。

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s test -p "test_*.py" -v
```

統合試験の構成と、main と PR で 10 回連続して成功した後に必須チェックへ追加する方針は、[CI での統合試験](docs/spec/12-ci-integration.md) にまとめています。
