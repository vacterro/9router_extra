# 9Router Extra / スキャナー＆統合スイート

バージョン: **v0.2.0**
プロジェクト: 9router_extra
パス: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## 概要
このリポジトリには、9Router 向けの拡張機能、ツール、状態バックアップ、パッチが含まれています：
1. **WorkBuddy AI（`wb/hy3`）プロバイダーブリッジ**
   - WorkBuddy AI のクラウド補完（`https://www.workbuddy.ai/v2/chat/completions`）との直接統合。
   - Hunyuan 推論モデル `hy3` に対応（`stream: true` を強制、型付きコンテンツブロック、`reasoning_effort: high`、`reasoning_summary: auto`）。
   - `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info` からのローカルトークン自動インポート。
   - トークンの更新とクォータ追跡に対応。
2. **Antigravity Gemini 3.8 Flash の修正と動的ティア解決**
   - upstream の 404 問題を修正：upstream の Antigravity は `gemini-3.8-flash-tiered(high|medium|low)` を要求します。
   - 動的なパターンマッチングを追加し、任意の `gemini-*-flash-(high|medium|low)` が 404 なしでティア付き形式にマップされます。
3. **SAIFREN コンボの先頭挿入**
   - `wb/hy3` を 1 番目のモデル、`ag/gemini-3.8-flash-high` を `SAIFREN` の 2 番目のモデルとして設定。
4. **AgentRouter と Cline のパッチ**
   - ネイティブな AgentRouter プロバイダー、クライアント User-Agent の転送、Anthropic ベース URL の解決、Cline ルーティングの修正。
5. **状態バックアップと移行エンジン**
   - 36 接続すべて、13 のカスタムノード（B.AI、Dahl、Vyce など）、およびコンボのスナップショット。

## 構成
- `apply-update.ps1` - 安全に停止し、バックアップし、v0.5.65-extra へアップグレードし、状態を復元して 9router を再起動する 1 クリック更新スクリプト。
- `packages/`
  - `9router-0.5.65-extra.tgz` - 完全にビルドされパッチ済みの v0.5.65 パッケージ。グローバルインストール可能。
  - `9router-0.5.59-agentrouter.tgz` - レガシーな v0.5.59 パッケージ。
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - v0.5.65 upstream に対する統合パッチ。
  - `9router-extra-unified.patch` - v0.5.59 に対する統合パッチ。
- `backup/`
  - `export_state.js` と `restore_state.js` - SQLite 状態のバックアップと同期ツール
    （プライベート状態の出力先は `%LOCALAPPDATA%\9router_WatchEdit\backups\private\` であり、このリポジトリには決して書き込みません）。

## 9Router ソーステストの実行（vitest 呼び出しの罠、T-21）
Next.js ダッシュボードのソースは `%APPDATA%\9router\source` にあります。その vitest の
パスエイリアス（`@/` → `src`、`open-sse` → `open-sse`）は
`tests/vitest.config.js` にのみ定義されています。

**ソースのルートからバイナリを呼び出さないでください：**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

代わりに、設定を読み込む呼び出しを使ってください：
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

テスト設定を再エクスポートするルート直下の `vitest.config.js` があれば、ルートの
バイナリも動作します。両方の呼び出しが一致するよう、このファイルはソースツリーに
保持されています。`tests/unit/model-routing.test.js` には、この環境で `os.tmpdir()`
の後処理時に無関係な既存の `EPERM` があります。これはどちらの呼び出しでも同じように
失敗し、エイリアスの問題ではありません。

## セキュリティモデル
このリポジトリは秘密情報を含まず、外部のコーディングエージェントに渡しても安全です。
秘密情報はローカルのランタイム層（`%LOCALAPPDATA%\9router_WatchEdit\` と
9Router のインストール先）にのみ存在します。WatchEdit は既定で `SECRETS: LOCKED` として動作し、
ライブのプロバイダー操作には明示的なロック解除（OS による許可、または任意のマスターパスワード保管庫）が必要です。
モデル全体、SAFE 共有ビルダー（`python tools/build_safe_share.py`）、シークレットスキャナー、
プライベートバックアップ用ツールについては **SECURITY_LOCAL.md** を参照してください。
  - `data-snapshot/` - `%APPDATA%\9router\db\data.sqlite` のデータベース全体のコピー。

## アップデートの適用方法
9router での進行中のタスクが終わったら、次を実行するだけです：
```powershell
.\apply-update.ps1
```
これは次のことを行います：
1. 実行中の 9router インスタンスを停止します。
2. `%APPDATA%\9router_backup_<timestamp>` にタイムスタンプ付きバックアップを作成します。
3. `9router-0.5.65-extra.tgz` をグローバルにインストールします。
4. すべての接続を同期し、SAIFREN コンボを更新します。
5. 9router をトレイモードで再起動します。

<!-- translation-model: codebuff-agent in-session translation (not the local qwen3:14b pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
