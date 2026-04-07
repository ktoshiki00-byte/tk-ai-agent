import io
import json
import os
import logging
import tempfile

import anthropic
from dotenv import load_dotenv
from flask import Flask, jsonify, request, abort
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
PDF_PREFIX     = 'PDF作成：'

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
# PDF生成 / Google Drive
# ─────────────────────────────────────

def generate_pdf_content(topic: str) -> str:
    """Claudeにトピックの企画書コンテンツを生成させる"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model='claude-sonnet-4-20250514',
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{'role': 'user', 'content': (
            f'「{topic}」について企画書を作成してください。\n'
            '見出し・本文・箇条書きを含む構造化された内容で、'
            'PDFに収まるよう2000文字以内でまとめてください。'
        )}],
    )
    return ''.join(block.text for block in msg.content if hasattr(block, 'text'))


def _wrap_lines(text: str, max_chars: int = 42) -> list:
    """テキストを指定文字数で折り返してリストで返す"""
    result = []
    for paragraph in text.split('\n'):
        if not paragraph:
            result.append('')
            continue
        while len(paragraph) > max_chars:
            result.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:]
        result.append(paragraph)
    return result


def create_pdf(title: str, content: str) -> str:
    """reportlabで日本語PDFを作成し一時ファイルパスを返す"""
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.lib.pagesizes import A4

    pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))

    tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
    tmp_path = tmp.name
    tmp.close()

    c = canvas.Canvas(tmp_path, pagesize=A4)
    width, height = A4
    margin = 50

    # タイトル
    c.setFont('HeiseiKakuGo-W5', 18)
    c.drawString(margin, height - 60, title)
    c.line(margin, height - 70, width - margin, height - 70)

    # 本文
    c.setFont('HeiseiKakuGo-W5', 11)
    y = height - 100
    line_height = 18

    for line in _wrap_lines(content):
        if y < margin + line_height:
            c.showPage()
            c.setFont('HeiseiKakuGo-W5', 11)
            y = height - margin
        c.drawString(margin, y, line)
        y -= line_height

    c.save()
    return tmp_path


def upload_to_drive(file_path: str, filename: str) -> str:
    """Google DriveにPDFをアップロードし共有リンクを返す"""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds_json = os.environ.get('GOOGLE_CREDENTIALS', '')
    if not creds_json:
        raise ValueError('GOOGLE_CREDENTIALS が設定されていません')

    creds_info = json.loads(creds_json)
    credentials = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=['https://www.googleapis.com/auth/drive.file'],
    )
    service = build('drive', 'v3', credentials=credentials)

    file_metadata = {'name': filename, 'parents': ['1LL94DCzWnvI-6L6k_3AcansFB1MnKZhz']}
    media = MediaFileUpload(file_path, mimetype='application/pdf')
    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id',
    ).execute()

    file_id = uploaded.get('id')
    service.permissions().create(
        fileId=file_id,
        body={'type': 'anyone', 'role': 'reader'},
    ).execute()

    return f'https://drive.google.com/file/d/{file_id}/view'


def handle_pdf_creation(topic: str) -> str:
    """PDF作成フロー全体を実行してLINEへ返す文字列を返す"""
    try:
        logger.info(f'PDF作成開始: {topic}')
        content  = generate_pdf_content(topic)
        filename = f'{topic}.pdf'
        pdf_path = create_pdf(topic, content)
        try:
            link = upload_to_drive(pdf_path, filename)
        finally:
            os.unlink(pdf_path)
        logger.info(f'PDF作成完了: {link}')
        return f'「{topic}」のPDFを作成しました！\n\n{link}'
    except Exception as e:
        logger.error(f'PDF作成エラー: {e}')
        return f'PDF作成中にエラーが発生しました。\n{e}'


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

    if text.startswith(PDF_PREFIX):
        topic = text[len(PDF_PREFIX):].strip()
        reply = handle_pdf_creation(topic)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    try:
        reply = ask_claude(user_id, text)
    except Exception as e:
        logger.error(f'Claude API エラー: {e}')
        reply = '少し待ってからもう一度お試しください。'

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


@app.route("/morning_report", methods=["GET", "POST"])
def morning_report():
    """朝のメッセージをプッシュ送信するエンドポイント（cronから呼び出される）"""
    logger.info('朝の挨拶送信開始')
    line_user_id = os.environ.get("LINE_USER_ID", "").strip()
    if not line_user_id:
        logger.warning('LINE_USER_ID が設定されていません')
        return jsonify({'status': 'skipped', 'message': 'LINE_USER_ID が設定されていません'}), 200
    target_ids = [line_user_id]

    for uid in target_ids:
        try:
            line_bot_api.push_message(uid, TextSendMessage(text='おはようございます！本日もよろしくお願いします。'))
            logger.info(f'送信完了: {uid}')
        except Exception as e:
            logger.error(f'送信失敗 ({uid}): {e}')

    return jsonify({'status': 'ok', 'message': '朝の挨拶を送信しました'})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
