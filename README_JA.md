# DeepAgents + ACP + FastAPI Gateway

このプロジェクトは、実行可能な ACP（Agent Client Protocol）Web 統合サンプルです。基本構成は次の通りです。

```text
ブラウザフロントエンド（ACP Client）
   ├─ POST /upload 画像/ファイルをアップロード → FastAPI ゲートウェイが保存し、HTTP リソース URL を返す
   └─ WS /acp-ws → app.py ゲートウェイ → acp_agent.py サブプロセス（stdio ACP）
                                  └─ DeepAgents Agent → OpenAI 互換モデルサービス
```

- **ACP 層**: `deepagents-acp` と `agent-client-protocol` を利用し、`session/update` や `session/request_permission` などのプロトコルメッセージを自動処理します。手動で JSON-RPC を書く必要はありません。
- **ゲートウェイ層**: `app.py` は `WebSocket ⇄ stdio` の双方向転送とファイルアップロードを担当し、ビジネスロジックそのものは解析しません。
- **フロントエンド**: `static/index.html` は、サードパーティ製ライブラリに依存しないネイティブの ACP WebSocket クライアントです。ACP v2 メッセージを直接送受信できます。

## ディレクトリ構成

```text
./
├─ app.py                # FastAPI ゲートウェイ（/upload + /acp-ws）
├─ acp_agent.py          # DeepAgents ACP agent（stdio サービス）
├─ utils/
│  └─ model_util.py     # モデル初期化設定（OpenAI 互換 API）
├─ static/
│  └─ index.html        # フロントエンドのデモページ
├─ uploads/              # アップロードファイル保存先（自動作成）
├─ .env                  # アプリ設定（HOST/PORT / モデル API）
├─ requirements.txt
├─ LICENSE
├─ README.md
├─ README_EN.md
├─ README_JA.md
└─ .venv/                # 任意のローカル仮想環境
```

## 現在の実装内容

このプロジェクトの実際のエントリーポイントは旧版 README にあった `gateway.py` ではなく、`app.py` です。

- `app.py` が FastAPI サービスを起動します。
- `@app.post("/upload")` がファイルを受け取り、`uploads/` に保存したうえで次を返します。
  - `uri`: アクセス可能な HTTP URL
  - `name`: ファイル名
  - `mimeType`: MIME タイプ
- `@app.websocket("/acp-ws")` は各 WebSocket 接続ごとに独立した `acp_agent.py` サブプロセスを作成し、双方向に転送します。
- `acp_agent.py` の `build_agent()` は `create_deep_agent(...)` と `interrupt_on` を使って権限確認を発火します。

## 起動手順

### 1) 仮想環境を作成し依存関係をインストール

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

### 2) 環境変数を設定

プロジェクトには `.env` が含まれており、例は次の通りです。

```env
# [Common Settings]
PYTHONIOENCODING=utf-8
APP_ENV=pro

MODEL_PROVIDER=openai

TAVILY_API_KEY=-
TIMEOUT=300
MAX_RETRY=2

# [Chat AI settings]
API_KEY=-
ENDPOINT=http://192.168.3.28:8088
MODEL_NAME=qwen3.5-9b
TEMPERATURE=0.8
TOP_P=0.5
MAX_TOKENS=32768
```

補足:
- `ENDPOINT` は OpenAI 互換 API のベース URL です。
- `API_KEY` はモデルサービスのアクセスキーです。
- `APP_HOST` / `APP_PORT` で FastAPI の待ち受け設定を制御します。

OpenAI 公式 API に直接接続する場合でも、同じ考え方で環境変数を設定してください。

### 3) ゲートウェイを起動

```powershell
.\.venv\Scripts\python app.py
```

デフォルトの待ち受け先:
- `http://127.0.0.1:8000`

### 4) フロントエンドページを開く

```text
http://127.0.0.1:8000/static/index.html
```

## フロントエンドの対話フロー

### ファイルアップロード

フロントエンド側で画像またはファイルを選択すると、次のようにアップロードを実行します。

```http
POST /upload
Content-Type: multipart/form-data
```

返却例:

```json
{
  "uri": "http://127.0.0.1:8000/uploads/xxx.png",
  "name": "xxx.png",
  "mimeType": "image/png"
}
```

その後、フロントエンドはこのリソースを `resource_link` として `session/prompt` のコンテンツ一覧に追加します。

### ACP セッションの流れ

フロントエンドは次の順序で処理を行います。

1. `initialize` — ACP 接続を確立しプロトコルバージョンを交渉する
2. `session/new` — 初回送信時に新規セッションを作成する
3. `session/prompt` — ユーザーのタスクと必要に応じたリソースを送信する
4. `session/update` — エージェントの進捗状態を受信し続ける
5. `session/request_permission` — エージェントがユーザー確認を必要としたときに承認ダイアログを表示する

## セッション再利用

フロントエンドにはマルチターン対話の再利用機能があります。

- 初回送信: 自動的に新しいセッションを作成
- 以降の送信: 現在の `sessionId` を再利用し、エージェントが過去の記憶を保持
- 「新規セッション」クリック: セッションをリセットし、古い記憶を破棄
- ページ更新や WebSocket 切断: 旧セッションは無効化され、次回送信時に自動で再作成

基本原理:
- 各 WebSocket 接続ごとに `acp_agent.py` のサブプロセスが起動する
- `AgentServerACP` が `sessionId` ごとに状態を保持する
- 同一接続で `session/prompt` を複数回呼ぶことでマルチターン対話が可能になる

## 権限承認メカニズム

`acp_agent.py` では `interrupt_on` を有効化しており、高リスク操作の前に HITL 割り込みを発生させ、その後 ACP の `session/request_permission` に変換します。

```python
interrupt_on={
    "execute": True,
    "delete": True,
}
```

フロントエンドは次の選択肢を持つ確認ダイアログを表示します。
- 許可
- 拒否
- 常に許可

返信例:

```json
{ "outcome": { "outcome": "selected", "optionId": "approve" } }
```

> フロントエンドは同じメッセージ ID で応答し、ACP のリクエスト/レスポンスの対応を保ちます。

## モデルとツール設定

モデル初期化は [utils/model_util.py](utils/model_util.py) にあります。

```python
MODEL = init_chat_model(
    MODEL_NAME,
    model_provider=MODEL_PROVIDER,
    base_url=ENDPOINT,
    api_key=API_KEY,
    timeout=TIMEOUT,
    max_retries=MAX_RETRY,
    temperature=TEMPERATURE,
    top_p=TOP_P,
    max_tokens=MAX_TOKENS,
)
```

つまり、このプロジェクトは OpenAI 互換サービスを利用しており、モデル名やエンドポイントは `.env` で簡単に調整できます。

## 代表的な利用例

### タスク: 画像を分析して設定を生成する

1. フロントエンドにタスクを入力する
   - 例: 「アップロードされた画像を分析し、プロジェクト設定を作成して」
2. 画像ファイルを選択する
3. 「Submit Task」をクリックする
4. エージェントは次を出力する
   - 実行計画
   - ストリーミングテキスト
   - ツール呼び出しカード
   - 最終まとめ

### タスク: シェルコマンドを実行する

エージェントがシェルコマンドを実行する必要がある場合、権限承認ダイアログが表示され、ユーザーが許可または拒否を選択します。

## よくある問題と対処法

### 1. 「Submit Task」を押しても反応がない

確認事項:
- バックエンドが起動しているか: `http://127.0.0.1:8000/docs` にアクセス可能か
- ページを Ctrl + F5 で再読込したか
- フロントエンドの JS ログにエラーが出ていないか

### 2. モデルが “Missing credentials” / “Connection error” を返す

一般的には次が原因です。
- `.env` の `API_KEY` または `ENDPOINT` が不正
- モデルサービスが起動していない、またはアドレスに到達できない
- OpenAI 互換サービスがリクエストを拒否している

### 3. ポートが既に使用されている

`.env` で次を変更してください。

```env
APP_PORT=8000
```

または、起動前にそのポートを使っているプロセスを停止してください。

## 既知の制約

- 各 WebSocket 接続ごとに独立したエージェントサブプロセスを起動するため、デモや単一ユーザー向けの利用に適している
- アップロードファイルに対する割り当て量や自動削除の仕組みはまだない
- 認証機構がなく、本番環境ではトークン検証を追加することを推奨する
- 現時点の例はデモ中心で、多セッションやマルチテナント構成へ拡張可能

## まとめ

今回の実装の中心は次の通りです。

- `app.py` が FastAPI ゲートウェイとして動作する
- `acp_agent.py` が ACP ランタイムとして動作する
- `static/index.html` が Web UI を提供する
- `.env` と [utils/model_util.py](utils/model_util.py) がモデル接続設定を担う

開発者にとって、このプロジェクトは ACP 連携の実例としても、Web とエージェントの橋渡しテンプレートとしても役立ちます。
