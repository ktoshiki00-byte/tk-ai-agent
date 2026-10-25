import io
import json
import os
import logging
import re
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import anthropic
from dotenv import load_dotenv
from flask import Flask, jsonify, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─────────────────────────────────────
# 状態確認（/status）用の記録
# ※ メモリ上の記録なので、Renderの再起動・スリープ復帰でリセットされる
# ─────────────────────────────────────

JST = timezone(timedelta(hours=9))
STARTED_AT = datetime.now(JST)

_status_lock = threading.Lock()
_error_times: deque = deque(maxlen=1000)   # ERRORログの発生時刻
_last_error = {"at": None, "message": None}
_last_morning_report = {"at": None, "result": None}
_last_line_message_at = None


class _ErrorCounter(logging.Handler):
    """ERROR以上のログを数えるだけのハンドラ（内容は先頭120字だけ保持）"""

    def emit(self, record):
        now = datetime.now(JST)
        with _status_lock:
            _error_times.append(now)
            _last_error["at"] = now
            # LINEユーザーID（U+32桁）は伏せる
            msg = re.sub(r"U[0-9a-f]{32}", "U***", record.getMessage())
            _last_error["message"] = msg[:120]


_error_counter = _ErrorCounter(level=logging.ERROR)
logging.getLogger().addHandler(_error_counter)


def _iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None

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
# PDF生成 / 一時ホスティング
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


def create_pdf(title: str, content: str, filename: str) -> str:
    """reportlabで日本語PDFを作成し /tmp に保存してパスを返す"""
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.lib.pagesizes import A4

    pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))

    pdf_path = os.path.join('/tmp', filename)

    c = canvas.Canvas(pdf_path, pagesize=A4)
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
    return pdf_path


def handle_pdf_creation(topic: str, base_url: str) -> str:
    """PDF作成フロー全体を実行してLINEへ返す文字列を返す"""
    try:
        logger.info(f'PDF作成開始: {topic}')
        content  = generate_pdf_content(topic)
        filename = f'{topic}.pdf'
        create_pdf(topic, content, filename)
        link = f'{base_url.rstrip("/")}/pdf/{filename}'
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


@app.route("/pdf/<path:filename>")
def serve_pdf(filename):
    """一時ホスティングしたPDFを配信する"""
    from flask import send_from_directory
    return send_from_directory('/tmp', filename, mimetype='application/pdf')


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
    global _last_line_message_at
    with _status_lock:
        _last_line_message_at = datetime.now(JST)
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
        topic    = text[len(PDF_PREFIX):].strip()
        base_url = os.environ.get('BASE_URL', request.host_url)
        reply    = handle_pdf_creation(topic, base_url)
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

    failed = 0
    for uid in target_ids:
        try:
            line_bot_api.push_message(uid, TextSendMessage(text='おはようございます！本日もよろしくお願いします。'))
            logger.info(f'送信完了: {uid}')
        except Exception as e:
            failed += 1
            logger.error(f'送信失敗 ({uid}): {e}')

    with _status_lock:
        _last_morning_report["at"] = datetime.now(JST)
        _last_morning_report["result"] = "ok" if failed == 0 else f"failed {failed}/{len(target_ids)}"

    return jsonify({'status': 'ok', 'message': '朝の挨拶を送信しました'})


# ─────────────────────────────────────
# 状態確認ページ
# 点検エージェントがログインなしで読むためのページ。
# 個人情報・会話内容・トークン値は出さない。
# ─────────────────────────────────────

_line_check_cache = {"at": 0.0, "result": None}


def _check_line_token() -> str:
    """LINEのアクセストークンが有効か確認（10分キャッシュ）"""
    now = time.time()
    if _line_check_cache["result"] and now - _line_check_cache["at"] < 600:
        return _line_check_cache["result"]
    try:
        line_bot_api.get_bot_info()
        result = "ok"
    except Exception as e:
        result = f"error: {type(e).__name__}"
    _line_check_cache.update(at=now, result=result)
    return result


@app.route("/status")
def status():
    now = datetime.now(JST)
    since = now - timedelta(hours=24)
    with _status_lock:
        errors_24h = sum(1 for t in _error_times if t >= since)
        last_error = dict(_last_error)
        last_mr = dict(_last_morning_report)
        last_msg = _last_line_message_at

    line_token = _check_line_token()
    config = {
        "ANTHROPIC_API_KEY": bool(ANTHROPIC_API_KEY),
        "LINE_CHANNEL_ACCESS_TOKEN": bool(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")),
        "LINE_CHANNEL_SECRET": bool(os.environ.get("LINE_CHANNEL_SECRET")),
        "LINE_USER_ID": bool(os.environ.get("LINE_USER_ID", "").strip()),
    }

    problems = []
    warnings = []
    if line_token != "ok":
        problems.append("LINEトークンが無効、またはLINEに接続できない")
    missing = [k for k, v in config.items() if not v]
    if missing:
        problems.append("環境変数が未設定: " + ", ".join(missing))
    if errors_24h:
        warnings.append(f"直近24時間のエラーログ {errors_24h} 件")
    if last_mr["result"] and last_mr["result"] != "ok":
        problems.append(f"朝レポートの送信失敗 ({last_mr['result']})")

    overall = "異常" if problems else ("注意" if warnings else "正常")

    return jsonify({
        "app": "tk-ai-agent",
        "overall": overall,
        "problems": problems,
        "warnings": warnings,
        "checked_at": _iso(now),
        "started_at": _iso(STARTED_AT),
        "uptime_minutes": int((now - STARTED_AT).total_seconds() // 60),
        "line_token": line_token,
        "config_present": config,
        "errors_24h": errors_24h,
        "last_error_at": _iso(last_error["at"]),
        "last_error_message": last_error["message"],
        "last_morning_report_at": _iso(last_mr["at"]),
        "last_morning_report_result": last_mr["result"],
        "last_line_message_at": _iso(last_msg),
        "note": "記録はメモリ上のため、再起動やスリープ復帰で started_at 以降の分だけになります",
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
