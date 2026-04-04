import os
import logging

import anthropic
from dotenv import load_dotenv
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
handler      = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ─────────────────────────────────────
# 定数
# ─────────────────────────────────────

SYSTEM_PROMPT = """あなたは丸利玉樹利喜蔵商店の営業戦略・企画開発AIアシスタントです。
役員・幹部と対話しながら意思決定をサポートします。

【重要ルール】
- 企画・提案は必ずWeb検索で最新情報を調べてから回答する
- 不確かな情報は「確認が必要です」と明示する
- 嘘・憶測での回答は絶対にしない
- 会話の文脈を踏まえて深掘りする

【会社情報】
- TAMAKI（商社・日本）年商約32億円→2025年度約9億円減で深刻な状況
- ミヤオ（四日市工場）年商約16億円・赤字
- MIYAWO（マレーシア工場）年商約8億円・赤字
- TOTSU（タイ工場）年商約2億円・赤字
- 主要取引先：MUJI・イオングループ（両チャネルとも2025年大幅減）
- EC年商約2億円（新規顧客獲得がボトルネック）

【緊急課題】
- 全工場赤字・TAMAKIの売上急減
- 大口卸依存からの脱却が最優先
- 直販チャネル構築（蔵前エリアでのポップアップ・直営店検討中）
- EC新規顧客獲得施策の具体化
- 製造直販モデルを強みとしたブランディング

【得意分野】
- 営業戦略の立案・ブラッシュアップ
- 新規チャネル・直販戦略の企画
- 企画書・提案書の作成補助
- 競合分析・市場トレンド（Web検索で最新情報を取得）
- 数値分析・売上改善提案"""

RESET_KEYWORDS = ('リセット', 'クリア', '新しい話題')
MAX_HISTORY    = 20

# ─────────────────────────────────────
# 会話履歴管理
# ─────────────────────────────────────

# { user_id: [{"role": "user"|"assistant", "content": str}, ...] }
conversation_history: dict = {}


# ─────────────────────────────────────
# Claude API
# ─────────────────────────────────────

def ask_claude(user_id: str, user_text: str) -> str:
    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    history = conversation_history.setdefault(user_id, [])

    history.append({"role": "user", "content": user_text})

    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=history,
    )

    reply = ''.join(
        block.text for block in msg.content
        if hasattr(block, 'text')
    )

    history.append({"role": "assistant", "content": reply})

    if len(history) > MAX_HISTORY:
        conversation_history[user_id] = history[-MAX_HISTORY:]

    return reply


# ─────────────────────────────────────
# Flask ルート
# ─────────────────────────────────────

@app.route("/")
def index():
    return "tk-ai-agent 稼働中"


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body      = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text    = event.message.text.strip()

    if text in RESET_KEYWORDS:
        conversation_history.pop(user_id, None)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text='会話履歴をリセットしました。新しい話題からどうぞ。'),
        )
        return

    try:
        reply = ask_claude(user_id, text)
    except Exception as e:
        logger.error(f'Claude API エラー: {e}')
        reply = '少し待ってからもう一度お試しください。'

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
