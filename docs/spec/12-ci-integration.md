# 12. CI での統合試験

## 目的

`test/manual/production_line.py` と `test/manual/negative_checks.py` を GitHub Actions 上のヘッドレス Factorio で自動実行する。Mod、RCON 通信、MCP サーバーの通信層の変更が、実機での歩行・採掘・製作・建設・受け渡しと、異常系の判定を壊していないことを PR ごとに確かめる。

手動の統合試験と CI の統合試験は同じスクリプトを使う。CI 専用の判定や、ゲームのルールを緩める設定は追加しない。

## 実現性の調査結果

2026-10-19 に `spike/ci-integration` ブランチの調査用ワークフローで確認した（[実行結果](https://github.com/menbou404/factorioMCP/actions/runs/39200815462)）。

| 確認項目 | 結果 |
| --- | --- |
| ヘッドレス版の取得 | `https://factorio.com/get-download/2.0.77/headless/linux64` から認証なしで取得できた（約 57 MB、約 5 秒） |
| 同梱 Mod | `data/` に `base`、`elevated-rails`、`quality`、`space-age` が含まれ、手元と同じ Mod 構成で起動できた |
| マップ作成・起動 | `--create` で新規マップを作成し、`--start-server` と `--rcon-bind 127.0.0.1:27277` で約 3 秒後に RCON が応答した |
| 正常系 | `PRODUCTION_LINE_SUCCEEDED 10`（125 秒） |
| 異常系 | `NEGATIVE_CHECKS_SUCCEEDED 13`（24 秒） |
| ジョブ全体 | 約 2 分 46 秒 |

本 Mod は Space Age に依存しない。品質指定の `normal` は基本ゲームにも存在する。

## 構成

```text
GitHub Actions (ubuntu-latest)
  integration ジョブ
    ├─ scripts/ci/start-headless.sh   ヘッドレス版の取得・Mod 構成・マップ作成・起動・RCON 待ち
    ├─ test/manual/production_line.py 試験場所の整地 → 素材配置 → 生産ラインの構築
    └─ test/manual/negative_checks.py 異常系の確認
```

- ワークフローは既存の `.github/workflows/ci.yml` に `integration` ジョブとして追加する。トリガーは既存ジョブと同じく、PR、main への push、手動実行とする。
- 変更ファイルによる実行の絞り込みは行わない。必須チェックに加えたとき、絞り込みで実行されなかったチェックが待機中のまま残るためである。
- 実行環境は Linux のみとする。Windows の起動スクリプトは既存の `powershell` ジョブで構文だけを確認する。

## 起動スクリプト `scripts/ci/start-headless.sh`

ワークフローから呼ぶ Bash スクリプトにまとめ、ワークフロー本体は手順の並びだけにする。処理順は次のとおり。

1. `FACTORIO_VERSION`（既定は `2.0.77`）のヘッドレス版を取得して展開する。取得済みのアーカイブがあれば再利用する。
2. `data/` に存在する公式 Mod（`elevated-rails`、`quality`、`space-age`）と本 Mod を有効にした `mod-list.json` を作業用の Mod フォルダに生成する。リポジトリの `mod/mod-list.json` は使わない。
3. 公開・LAN 掲載を無効にし、自動保存と自動一時停止を止めたサーバー設定を生成する。
4. ランダムな RCON パスワードを `.factorio-test/rcon-secret.txt` に書く。手動の統合試験と同じ場所であり、試験スクリプトは変更なしで動く。
5. `--create` で新規マップを作成し、`--start-server` でサーバーを起動する。ログはファイルに保存する。
6. ログに `Starting RCON interface` が出るまで最大 90 秒待つ。サーバーが終了した場合や時間切れの場合は、ログを出力して失敗する。

RCON は `127.0.0.1:27277` だけで待ち受ける。パスワードはログに出さない。

## 試験場所の整地

新規マップの原点付近には、木・岩・水・資源・敵が置かれることがある。`production_line.py` の素材配置の前に、次を `/c` で行う。

- 原点を中心とした 60 × 60 マスの範囲から、キャラクター以外のエンティティを取り除く
- 同じ範囲の地面を `grass-1` にする
- 地表を平和モードにし、既存の敵ユニットを消す

整地は CI 専用にせず、`production_line.py` の素材配置に含める。手動の統合試験も同じ手順になる。どちらの試験も使い捨てのセーブだけで実行する前提は変わらない。

## キャッシュと失敗時の資料

- 取得したアーカイブは `actions/cache` で保存する。キーにはバージョンを含め、バージョン変更時に取り直す。
- 失敗時はサーバーログを `actions/upload-artifact` で保存する。Factorio 本体、セーブ、RCON パスワードは保存しない。

## 必須チェックへの追加

試験は実時間の待機（炉の製錬完了など）に依存する。遅い実行環境では時間切れで失敗する可能性がある。そのため、最初は必須チェックに含めずに運用する。main と PR で 10 回連続して失敗なく通った時点で、ブランチ保護の必須チェックに `integration` を追加する。

途中で環境起因の失敗が出た場合は、待機時間を延ばして回数を数え直す。ゲームのルールを緩めて通すことはしない。

## 運用上の注意

- Factorio 本体は公式サイトから実行時に取得するだけにする。リポジトリ、成果物、コンテナイメージに本体を含めて再配布しない。
- CI の `FACTORIO_VERSION` は、手元の検証環境（`scripts/start-factorio.ps1` の Factorio 2.0.77）と合わせて更新する。
- 画面付きマルチプレイ主催での接続（P1）と人間の画面での観察（P2）は、CI の対象外である。手動で確認する。

## 実装時の受け入れ条件

- `integration` ジョブが PR 上で成功し、ログに `PRODUCTION_LINE_SUCCEEDED` と `NEGATIVE_CHECKS_SUCCEEDED` が出る。
- 既存の `lint`、`test`、`mod`、`powershell` ジョブが引き続き成功する。
- 手元の隔離環境（`.factorio-test`）でも、整地を含む `production_line.py` と `negative_checks.py` が成功する。
- 調査用の `spike/ci-integration` ブランチと調査用ワークフローは、本実装の PR がマージされた後に削除する。
