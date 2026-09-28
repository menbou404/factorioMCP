# 00. 確認済みの実行環境

## Factorio

| 項目 | 確認結果 |
| --- | --- |
| 実際のゲームフォルダ | `E:\factorio-space-forMCP\Factorio_2.0.77` |
| 実行ファイル | `bin\x64\factorio.exe` |
| 実行ファイルのバージョン | 2.0.77 |
| 有効な組み込み Mod | `base`、`space-age`、`quality`、`elevated-rails` |
| 同梱の API 資料 | `doc-html` |
| 設定ファイル | `config\config.ini` |
| ローカル RCON | 起動スクリプトが画面付き主催用の `local-rcon-socket` と `local-rcon-password` を作業用設定ファイルに指定。ゲーム本体の `config.ini` は未変更 |
| 検証用セーブ | `saves\NewGameForMCP.zip` の存在を確認 |

利用者が書いたパスは `Factorio\_2.0.77` と区切られていたが、実際に存在するフォルダ名は `Factorio_2.0.77` である。ポータブル版の `config-path.cfg` は設定フォルダをこのゲームフォルダ内の `config` に向けている。Windows 共通の `%APPDATA%\Factorio` をこの環境の設定先として使わない。

## 実装時の API 照合

同梱の `doc-html/classes` で次の API 名の存在を確認した: `LuaControl.get_main_inventory`、`LuaControl.walking_state`、`LuaControl.mining_state`、`LuaControl.begin_crafting`、`LuaSurface.request_path`、`LuaSurface.create_entity`、`LuaCommandProcessor.add_command`、`LuaRCON.print`。存在の確認は独立キャラクターでの実動作を保証しない。実動作は [09-validation.md](09-validation.md) の P1～P6 で検証する。

## 準備状態

- Factorio の配置とバージョン確認は完了。
- Mod と MCP サーバーの初期実装は完了。
- MCP サーバーは Python 3.12 と Python MCP SDK 2 系、RCON クライアントで構成する。Factorio 内の Mod は Lua のまま維持する。
- 隔離したヘッドレスマルチプレイ環境で、ローカル RCON、Mod、MCP の通信を検証済み。
- `NewGameForMCP` のコピーで Mod の読み込みとキャラクター生成・保存復元を検証済み。元のセーブは未変更。画面付きマルチプレイ主催での MCP 接続は検証済み。人間の画面で AI の歩行を観察する条件は未確認。
- 利用者が伝えたパスワードは本文に保存しない。ゲーム参加用パスワードと RCON 用パスワードは別設定として扱う。
