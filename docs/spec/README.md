# Factorio AI エージェント仕様書

## 目的と決定事項

画面内に存在する独立したキャラクターを AI が操作し、通常のゲームの制約を守りながら素材を集め、製作し、工場を建てる。最初は 1 体で動作させる。将来は同じマップで複数の AI が別々のキャラクターを操作する。

最初の構成は、同じローカル PC で起動する「画面付き Factorio のマルチプレイ主催」「Factorio 用 Mod」「MCP サーバー」である。利用者は主催中のゲーム画面で AI を観察する。MCP サーバーは Factorio のローカル RCON に接続する。AI は Factorio の接続プレイヤーではなく、Mod が作成するキャラクターを操作する。

## 文書一覧

| 文書 | 主な内容 |
| --- | --- |
| [00-environment.md](00-environment.md) | 確認済みの Factorio 環境と準備状態 |
| [01-architecture.md](01-architecture.md) | 構成、接続、信頼境界 |
| [02-character.md](02-character.md) | キャラクターの生成、識別、保存、死亡 |
| [03-observation.md](03-observation.md) | AI が取得できる周囲・持ち物・製作・研究情報 |
| [04-movement.md](04-movement.md) | 歩行、経路、障害物、停止 |
| [05-mining.md](05-mining.md) | 採掘と持ち物への反映 |
| [06-crafting.md](06-crafting.md) | 手作業の製作と材料消費 |
| [07-building.md](07-building.md) | 建設、材料消費、配置検証 |
| [11-item-transfer.md](11-item-transfer.md) | 設備とキャラクターの間のアイテム移動 |
| [08-mcp-interface.md](08-mcp-interface.md) | MCP ツール、RCON 通信、操作の状態 |
| [09-validation.md](09-validation.md) | 検証順序と受け入れ条件 |
| [10-multi-agent.md](10-multi-agent.md) | 複数 AI への拡張境界 |
| [12-ci-integration.md](12-ci-integration.md) | CI でのヘッドレス Factorio を使った統合試験 |
| [13-factory-roadmap.md](13-factory-roadmap.md) | 工場づくりへの拡張計画（M1〜M5）と各段階の設計 |
| [14-agent-ergonomics.md](14-agent-ergonomics.md) | AI エージェント向けの使いやすさ改善（M6） |
| [15-agent-trial-fixes.md](15-agent-trial-fixes.md) | 自律試行で見つかった問題の修正（M7） |
| [16-trial3-fixes.md](16-trial3-fixes.md) | 3 回目の試行で見つかった問題の修正（M8） |
| [17-trial4-fixes.md](17-trial4-fixes.md) | 4 回目の試行で見つかった問題の修正（M9） |
| [18-trial5-fixes.md](18-trial5-fixes.md) | 5 回目の試行で見つかった問題の修正（M10） |
| [19-trial6-fixes.md](19-trial6-fixes.md) | 6 回目の試行で見つかった問題の修正（M11） |
| [20-trial7-fixes.md](20-trial7-fixes.md) | 7 回目の試行で残った指摘の修正と探索済みの地図（M12） |
| [21-trial8-fixes.md](21-trial8-fixes.md) | 8 回目の試行で見つかった問題の修正（M13） |
| [22-green-science.md](22-green-science.md) | 緑サイエンスの自動化（M14） |
| [23-trial9-fixes.md](23-trial9-fixes.md) | 9 回目の試行で見つかった問題の修正（M15） |
| [24-trial10-fixes.md](24-trial10-fixes.md) | 10 回目の試行で見つかった問題の修正（M16） |
| [25-trial11-fixes.md](25-trial11-fixes.md) | 11 回目の試行で見つかった問題の修正（M17） |
| [26-trial12-fixes.md](26-trial12-fixes.md) | 12 回目の試行で見つかった問題の修正（M18） |
| [27-trial13-fixes.md](27-trial13-fixes.md) | 13 回目の試行で見つかった問題の修正（M19） |
| [28-trial14-fixes.md](28-trial14-fixes.md) | 14 回目の試行で見つかった問題の修正（M20） |
| [29-trial15-fixes.md](29-trial15-fixes.md) | 15 回目の試行で見つかった問題の修正（M21） |
| [30-trial16-fixes.md](30-trial16-fixes.md) | 16 回目の試行で見つかった問題の修正（M22） |
| [31-trial17-fixes.md](31-trial17-fixes.md) | 17 回目の試行で見つかった問題の修正（M23） |
| [32-hands-off-automation.md](32-hands-off-automation.md) | 手運びなしの自動化に向けた修正（M24） |
| [33-trial19-fixes.md](33-trial19-fixes.md) | 19 回目の試行で見つかった問題の修正（M25） |
| [34-trial20-fixes.md](34-trial20-fixes.md) | 20 回目の試行で見つかった問題の修正（M26） |
| [35-trial21-fixes.md](35-trial21-fixes.md) | 21 回目の試行で見つかった問題の修正（M27） |

## 共通の不変条件

1. AI の持ち物は、AI が操作するキャラクターのゲーム内インベントリである。MCP 側にゲーム内アイテムの正本を持たない。
2. アイテムは採掘、製作、設備からの回収など、仕様で認めたゲーム内の取得元からのみ増える。初期持ち物は空とし、Mod は支給しない。
3. 建設に必要なアイテムを持ち物から消費する。設置位置、衝突、作業距離、所属勢力を検証する。
4. 移動は歩行による。座標の直接変更やテレポートを移動ツールに使わない。
5. AI に提供する現在の世界情報は、キャラクターの周囲、本人の持ち物、製作可能なもの、所属勢力の既知の研究状況に限る。
6. ゲーム内操作の成否は Mod が返す実測結果で判定する。MCP サーバーは成功を推測して確定しない。
7. MCP に任意の Lua 実行、任意の RCON コマンド送信、アイテム付与、無償の設備生成を公開しない。

## 対象範囲

- 対象は利用者が用意した Factorio 2.0.77。Space Age、quality、elevated-rails が有効な環境で、最初の行動と設備は Nauvis 上の基本ゲーム相当のものに限定する。Space Age 固有の地形・品質・宇宙設備は初期対象外とする。
- 最初の完成単位は「1 体の AI が歩行し、通常の時間をかけて資源を採掘し、そのキャラクターの持ち物が増える」。その後に製作、建設、稼働する小規模な生産ラインへ広げる。
- AI の思考、目標分解、会話、モデル選択は MCP サーバーの外側にある AI クライアントが担う。MCP サーバーは観察と実行の道具を提供する。
- 人間プレイヤーはゲーム内に参加して観察できる。初期の受け入れ試験では、資材供給や建設による介入を行わない。

## 仕様の状態

この文書群は実装前の仕様である。API で可能なことと、独立キャラクターで実際に期待どおり動くことは区別する。[09-validation.md](09-validation.md) の技術検証が失敗した場合は、ゲームのルールを緩めずに実装方法と仕様を更新する。各機能末尾のオンライン API リンクは概念の参照先であり、実装時の正本は用意された 2.0.77 に同梱の `doc-html` とする。

## 根拠資料

- [Factorio Runtime API](https://lua-api.factorio.com/latest/index-runtime.html)
- [Factorio Mod の構成](https://lua-api.factorio.com/latest/auxiliary/mod-structure.html)
- [Factorio の RCON 起動オプション](https://wiki.factorio.com/Command_line_parameters)
- [画面付きマルチプレイ主催時のローカル RCON に関する開発者回答](https://forums.factorio.com/viewtopic.php?p=201757)
- [MCP TypeScript SDK](https://ts.sdk.modelcontextprotocol.io/v2/)
