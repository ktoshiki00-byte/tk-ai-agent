import calendar
import os
import json
import logging
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from flask import Flask, request, abort, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage as V3TextMessage,
    QuickReply,
    QuickReplyItem,
    PostbackAction,
)
import anthropic
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── LINE SDK v2（既存 webhook ハンドラ用） ───
line_bot_api = LineBotApi(os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
handler      = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])

# ─── LINE SDK v3（移植した daily-report 関数用） ───
configuration = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ─── Google Sheets 設定 ───
GOOGLE_SHEET_ID    = os.environ.get('GOOGLE_SHEET_ID', '')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS', '')
SHEETS_ENABLED     = bool(GOOGLE_SHEET_ID and GOOGLE_CREDENTIALS)
GOOGLE_SCOPES      = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive',
]

LINE_USER_ID = os.environ.get('LINE_USER_ID', '')
JST          = ZoneInfo('Asia/Tokyo')

# ─── 定数 ───
ACTION_EMOJI = {
    '商談':              '',
    '移動・外出':        '',
    'メーカー訪問':      '',
    '展示会・イベント':  '',
    '社内作業':          '',
    '工場対応':          '',
}
ACTION_SHORT = {
    '移動・外出':       '移動',
    '展示会・イベント': '展示会',
}

# ─── ユーザー状態管理 ───
user_states: dict = {}


# ─────────────────────────────────────
# Google Sheets ヘルパー
# ─────────────────────────────────────

def get_spreadsheet():
    credentials_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(credentials_dict, scopes=GOOGLE_SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID)


def get_or_create_sheet(spreadsheet, sheet_name: str, headers: list = None):
    try:
        sheet = spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=20)
        if headers:
            sheet.append_row(headers)
    return sheet


def save_report(
    display_name: str,
    time_slot: str,
    action: str,
    company: str = '',
    destination: str = '',
    work_content: str = '',
    factory_content: str = '',
    memo: str = '',
):
    now = datetime.now(JST)
    row = [
        now.strftime('%Y/%m/%d'),
        now.strftime('%H:%M'),
        display_name,
        time_slot,
        action,
        company,
        destination,
        work_content,
        factory_content,
        memo,
    ]
    if not SHEETS_ENABLED:
        logger.info(f'[SHEETS無効] 日報データ: {row}')
        return
    spreadsheet = get_spreadsheet()
    sheet = get_or_create_sheet(
        spreadsheet, '日報',
        ['日付', '時間', 'ユーザー名', '午前or午後', '行動種別',
         '訪問先会社名', '移動先', '作業内容', '工場対応内容', '自由メモ']
    )
    sheet.append_row(row)
    logger.info(f'日報保存完了: {display_name} / {time_slot} / {action}')


def get_display_name(user_id: str) -> str:
    try:
        with ApiClient(configuration) as api_client:
            profile = MessagingApi(api_client).get_profile(user_id)
            return profile.display_name
    except Exception as e:
        logger.error(f'プロフィール取得エラー ({user_id}): {e}')
        return '名無し'


# ─────────────────────────────────────
# Claude API
# ─────────────────────────────────────

def ask_claude(user_text):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": user_text}]
    )
    return msg.content[0].text


# ─────────────────────────────────────
# サマリーテキスト生成
# ─────────────────────────────────────

def build_summary(state: dict, display_name: str) -> str:
    lines = [
        '記録しました！',
        '━━━━━━━━━━━',
        f'{display_name}',
        f'{state["time_slot"]}',
        f'{state["action"]}',
    ]
    if state.get('company'):
        lines.append(f'訪問先: {state["company"]}')
    if state.get('destination'):
        lines.append(f'移動先: {state["destination"]}')
    if state.get('work_content'):
        lines.append(f'作業内容: {state["work_content"]}')
    if state.get('factory_content'):
        lines.append(f'対応内容: {state["factory_content"]}')
    if state.get('memo'):
        lines.append(f'メモ: {state["memo"]}')
    return '\n'.join(lines)


def get_next_state(current_state: str, action: str) -> str | None:
    if current_state == 'waiting_for_company':
        return 'waiting_for_memo'
    return None


def finalize_and_save(user_id: str, display_name: str):
    s = user_states[user_id]
    save_report(
        display_name    = display_name,
        time_slot       = s['time_slot'],
        action          = s['action'],
        company         = s.get('company', ''),
        destination     = s.get('destination', ''),
        work_content    = s.get('work_content', ''),
        factory_content = s.get('factory_content', ''),
        memo            = s.get('memo', ''),
    )


# ─────────────────────────────────────
# レポート用ヘルパー
# ─────────────────────────────────────

def format_action_label(row: dict, with_emoji: bool = False) -> str:
    action      = row.get('行動種別', '')
    company     = row.get('訪問先会社名', '').strip()
    destination = row.get('移動先', '').strip()
    emoji = ACTION_EMOJI.get(action, '') if with_emoji else ''
    short = ACTION_SHORT.get(action, action)
    if company:
        return f'{emoji}{short}/{company}'
    if destination:
        return f'{emoji}{short}/{destination}'
    return f'{emoji}{short}'


def get_all_users() -> list[dict]:
    if not SHEETS_ENABLED:
        logger.info('[SHEETS無効] ユーザー一覧の取得をスキップ')
        return []
    spreadsheet = get_spreadsheet()
    users_sheet = get_or_create_sheet(spreadsheet, 'users', ['LINE表示名', 'ユーザーID'])
    records = users_sheet.get_all_records()
    return [
        {'name': r['LINE表示名'], 'id': r['ユーザーID']}
        for r in records if r.get('ユーザーID')
    ]


def get_reports_by_date_range(date_strs: list[str]) -> list[dict]:
    if not SHEETS_ENABLED:
        return []
    spreadsheet = get_spreadsheet()
    report_sheet = get_or_create_sheet(
        spreadsheet, '日報',
        ['日付', '時間', 'ユーザー名', '午前or午後', '行動種別',
         '訪問先会社名', '移動先', '作業内容', '工場対応内容', '自由メモ']
    )
    all_records = report_sheet.get_all_records()
    date_set = set(date_strs)
    return [r for r in all_records if r.get('日付') in date_set]


def get_admins() -> list[dict]:
    if not SHEETS_ENABLED:
        if LINE_USER_ID:
            return [{'id': LINE_USER_ID, 'name': '管理者'}]
        return []
    spreadsheet = get_spreadsheet()
    admin_sheet = get_or_create_sheet(spreadsheet, '管理者', ['LINE_USER_ID', '名前'])
    records = admin_sheet.get_all_records()
    return [
        {'id': r['LINE_USER_ID'], 'name': r.get('名前', '')}
        for r in records if r.get('LINE_USER_ID')
    ]


def get_admin_ids() -> list[str]:
    return [a['id'] for a in get_admins()]


def is_admin(user_id: str) -> bool:
    return user_id in get_admin_ids()


def _push_to_admins(message: str):
    admin_ids = get_admin_ids()
    if not admin_ids:
        logger.warning('管理者が未登録のため送信をスキップ')
        return
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for uid in admin_ids:
            try:
                api.push_message(PushMessageRequest(
                    to=uid,
                    messages=[V3TextMessage(text=message)],
                ))
            except Exception as e:
                logger.error(f'管理者へのプッシュ失敗 ({uid}): {e}')


# ─────────────────────────────────────
# 有給・休暇申請ヘルパー
# ─────────────────────────────────────

def _calculate_leave_entitlement(hire_date: date) -> int:
    today  = datetime.now(JST).date()
    months = (today.year - hire_date.year) * 12 + (today.month - hire_date.month)
    if today.day < hire_date.day:
        months -= 1
    if months < 6:   return 0
    if months < 18:  return 10
    if months < 30:  return 11
    if months < 42:  return 12
    if months < 54:  return 14
    if months < 66:  return 16
    if months < 78:  return 18
    return 20


def get_leave_balance_sheet():
    spreadsheet = get_spreadsheet()
    return get_or_create_sheet(
        spreadsheet, '有給管理',
        ['名前', 'LINE_USER_ID', '入社日', '付与日数', '使用日数', '残日数', '最終付与日'],
    )


def get_leave_application_sheet():
    spreadsheet = get_spreadsheet()
    return get_or_create_sheet(
        spreadsheet, '休暇申請',
        ['申請日時', '名前', '申請種別', '開始日', '終了日', '日数', '理由',
         'ステータス', '承認者', '承認日時'],
    )


def _get_user_id_by_name(name: str) -> str:
    for u in get_all_users():
        if u['name'] == name:
            return u['id']
    return ''


def _get_leave_balance(user_id: str) -> dict | None:
    sheet   = get_leave_balance_sheet()
    records = sheet.get_all_records()
    for i, r in enumerate(records):
        if r.get('LINE_USER_ID') == user_id:
            return {'row': i + 2, 'data': r}
    return None


def _submit_leave_application(
    display_name: str, leave_type: str,
    start_date: str, end_date: str, days: int, reason: str,
) -> int:
    sheet       = get_leave_application_sheet()
    rows_before = len(sheet.get_all_records())
    now_str     = datetime.now(JST).strftime('%Y/%m/%d %H:%M')
    sheet.append_row([now_str, display_name, leave_type,
                      start_date, end_date, days, reason, '申請中', '', ''])
    return rows_before + 2


def _notify_admins_leave(
    display_name: str, leave_type: str,
    start_date: str, end_date: str, days: int, reason: str, row_num: int,
):
    admin_ids = get_admin_ids()
    if not admin_ids:
        logger.warning('管理者未登録のため休暇申請通知をスキップ')
        return
    msg_text = (
        f'休暇申請が届きました\n\n'
        f'申請者：{display_name}\n'
        f'種別：{leave_type}\n'
        f'期間：{start_date}〜{end_date}（{days}日）\n'
        f'理由：{reason}'
    )
    quick_reply = QuickReply(items=[
        QuickReplyItem(action=PostbackAction(
            label='承認',
            data=f'action=leave_approve&row={row_num}',
            display_text='承認',
        )),
        QuickReplyItem(action=PostbackAction(
            label='却下',
            data=f'action=leave_reject&row={row_num}',
            display_text='却下',
        )),
    ])
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for uid in admin_ids:
            try:
                api.push_message(PushMessageRequest(
                    to=uid,
                    messages=[V3TextMessage(text=msg_text, quick_reply=quick_reply)],
                ))
            except Exception as e:
                logger.error(f'申請通知送信失敗 ({uid}): {e}')


def _process_leave_decision(row_num: int, admin_id: str, approved: bool) -> str:
    try:
        app_sheet = get_leave_application_sheet()
        row_data  = app_sheet.row_values(row_num)
        if not row_data or len(row_data) < 7:
            return '申請データが見つかりません。'

        current_status = row_data[7] if len(row_data) > 7 else ''
        if current_status in ('承認', '却下'):
            return f'この申請はすでに{current_status}済みです。'

        app_name   = row_data[1]
        app_type   = row_data[2]
        start_date = row_data[3]
        end_date   = row_data[4]
        try:
            app_days = int(float(row_data[5]))
        except (ValueError, IndexError):
            app_days = 0

        new_status = '承認' if approved else '却下'
        admin_name = get_display_name(admin_id)
        now_str    = datetime.now(JST).strftime('%Y/%m/%d %H:%M')

        app_sheet.update_cell(row_num, 8,  new_status)
        app_sheet.update_cell(row_num, 9,  admin_name)
        app_sheet.update_cell(row_num, 10, now_str)

        if approved and app_type == '有給' and app_days > 0:
            bal_sheet   = get_leave_balance_sheet()
            bal_records = bal_sheet.get_all_records()
            for i, r in enumerate(bal_records):
                if r.get('名前') == app_name:
                    used      = int(float(str(r.get('使用日数', 0)))) + app_days
                    remaining = int(float(str(r.get('残日数', 0)))) - app_days
                    bal_sheet.update_cell(i + 2, 5, used)
                    bal_sheet.update_cell(i + 2, 6, remaining)
                    break

        applicant_id = _get_user_id_by_name(app_name)
        if applicant_id:
            if approved:
                notify_text = (
                    f'休暇申請が承認されました。\n'
                    f'種別：{app_type}\n'
                    f'期間：{start_date}〜{end_date}（{app_days}日）\n'
                    f'承認者：{admin_name}'
                )
            else:
                notify_text = (
                    f'休暇申請が却下されました。\n'
                    f'種別：{app_type}\n'
                    f'期間：{start_date}〜{end_date}\n'
                    f'担当者にご確認ください。'
                )
            try:
                with ApiClient(configuration) as api_client:
                    MessagingApi(api_client).push_message(PushMessageRequest(
                        to=applicant_id,
                        messages=[V3TextMessage(text=notify_text)],
                    ))
            except Exception as e:
                logger.error(f'申請者への結果通知失敗 ({applicant_id}): {e}')

        return f'{app_name}さんの申請を{new_status}しました。'

    except Exception as e:
        logger.error(f'申請決裁処理エラー: {e}')
        return 'エラーが発生しました。もう一度お試しください。'


def _send_daily_report():
    if not SHEETS_ENABLED:
        logger.info('日報レポートをスキップ（スプレッドシート未設定）')
        return
    admin_ids = get_admin_ids()
    if not admin_ids:
        logger.info('日報レポートをスキップ（管理者未登録）')
        return

    today      = datetime.now(JST)
    today_str  = today.strftime('%Y/%m/%d')
    date_label = f'{today.month}月{today.day}日'

    users         = get_all_users()
    today_records = get_reports_by_date_range([today_str])

    user_report_map: dict[str, dict] = {}
    for record in today_records:
        name = record.get('ユーザー名', '')
        slot = record.get('午前or午後', '')
        if name not in user_report_map:
            user_report_map[name] = {}
        user_report_map[name][slot] = record

    submitted, not_submitted = [], []
    for user in users:
        name = user['name']
        if name in user_report_map:
            submitted.append((name, user_report_map[name]))
        else:
            not_submitted.append(name)

    lines = [f'本日の日報レポート（{date_label}）', '']
    lines.append(f'提出済み（{len(submitted)}名）')
    for name, slots in submitted:
        parts = [
            f'{slot}:{format_action_label(slots[slot])}'
            for slot in ['午前', '午後'] if slot in slots
        ]
        lines.append(f'・{name}｜{" ".join(parts)}')
    lines.append('')
    lines.append(f'未提出（{len(not_submitted)}名）')
    for name in not_submitted:
        lines.append(f'・{name}')

    _push_to_admins('\n'.join(lines))
    logger.info('日報レポート送信完了')


def _send_weekly_report():
    if not SHEETS_ENABLED:
        logger.info('週次レポートをスキップ（スプレッドシート未設定）')
        return

    today  = datetime.now(JST)
    monday = today - timedelta(days=today.weekday())
    week_dates     = [monday + timedelta(days=i) for i in range(5)]
    week_date_strs = [d.strftime('%Y/%m/%d') for d in week_dates]
    week_label = (
        f'{monday.month}/{monday.day}〜'
        f'{week_dates[-1].month}/{week_dates[-1].day}'
    )

    users        = get_all_users()
    week_records = get_reports_by_date_range(week_date_strs)

    if not users:
        logger.info('週次レポートをスキップ（登録ユーザーが0名）')
        return

    DAY_NAMES           = ['月', '火', '水', '木', '金']
    admin_summary_lines: list[str] = []

    with ApiClient(configuration) as api_client:
        line_bot_api_v3 = MessagingApi(api_client)

        for user in users:
            name = user['name']
            uid  = user['id']

            user_week: dict[str, dict] = {}
            for record in week_records:
                if record.get('ユーザー名') != name:
                    continue
                d    = record.get('日付', '')
                slot = record.get('午前or午後', '')
                if d not in user_week:
                    user_week[d] = {}
                user_week[d][slot] = record

            day_lines: list[str]      = []
            missing_streak: list[str] = []
            submitted_days = 0

            for day_name, date_str in zip(DAY_NAMES, week_date_strs):
                slots  = user_week.get(date_str, {})
                has_am = '午前' in slots
                has_pm = '午後' in slots

                if not has_am and not has_pm:
                    missing_streak.append(day_name)
                else:
                    submitted_days += 1
                    if missing_streak:
                        day_lines.append(
                            f'{missing_streak[0]}｜未入力' if len(missing_streak) == 1
                            else f'{missing_streak[0]}〜{missing_streak[-1]}｜未入力'
                        )
                        missing_streak = []
                    am_text = format_action_label(slots['午前'], with_emoji=True) if has_am else '未入力'
                    pm_text = format_action_label(slots['午後'], with_emoji=True) if has_pm else '未入力'
                    day_lines.append(f'{day_name}｜午前:{am_text} 午後:{pm_text}')

            if missing_streak:
                day_lines.append(
                    f'{missing_streak[0]}｜未入力' if len(missing_streak) == 1
                    else f'{missing_streak[0]}〜{missing_streak[-1]}｜未入力'
                )

            negotiation_count = sum(
                1 for r in week_records
                if r.get('ユーザー名') == name and r.get('行動種別') == '商談'
            )

            action_counter = Counter(
                r.get('行動種別', '')
                for r in week_records
                if r.get('ユーザー名') == name and r.get('行動種別')
            )
            activity_lines = [
                f'　{action}：{count}件'
                for action, count in action_counter.most_common()
            ]
            activity_text = '\n'.join(activity_lines) if activity_lines else '　（データなし）'

            message = (
                f'今週の振り返り（{week_label}）\n{name}さん\n\n'
                + '\n'.join(day_lines)
                + f'\n\n今週の商談件数：{negotiation_count}件'
                + f'\n\n主な活動内容\n{activity_text}'
            )

            try:
                line_bot_api_v3.push_message(PushMessageRequest(
                    to=uid,
                    messages=[V3TextMessage(text=message)],
                ))
                logger.info(f'週次レポート送信完了: {name} ({uid})')
            except Exception as e:
                logger.error(f'週次レポート送信失敗 ({uid}): {e}')

            admin_summary_lines.append(
                f'・{name}｜提出{submitted_days}日 商談{negotiation_count}件'
            )

    if admin_summary_lines:
        admin_message = (
            f'今週の週次サマリー（{week_label}）\n\n'
            + '\n'.join(admin_summary_lines)
        )
        try:
            _push_to_admins(admin_message)
            logger.info('週次管理者サマリー送信完了')
        except Exception as e:
            logger.error(f'週次管理者サマリー送信失敗: {e}')


def _build_report_text(name: str, date_sheet: str, date_label: str) -> str:
    records      = get_reports_by_date_range([date_sheet])
    user_records = [r for r in records if r.get('ユーザー名') == name]
    if not user_records:
        return f'{name}さんの{date_label}の日報\n\n未提出です。'
    lines = [f'{name}さんの{date_label}の日報\n']
    for rec in sorted(user_records, key=lambda r: r.get('午前or午後', '')):
        slot  = rec.get('午前or午後', '')
        label = format_action_label(rec, with_emoji=True)
        lines.append(f'{slot}：{label}')
    return '\n'.join(lines)


# ─────────────────────────────────────
# 期間照会ヘルパー
# ─────────────────────────────────────

def _generate_date_list(start: date, end: date) -> list[str]:
    result  = []
    current = start
    while current <= end:
        result.append(current.strftime('%Y/%m/%d'))
        current += timedelta(days=1)
    return result


def _get_month_dates(year: int, month: int) -> tuple[list[str], str]:
    _, last_day = calendar.monthrange(year, month)
    first = date(year, month, 1)
    last  = date(year, month, last_day)
    return _generate_date_list(first, last), f'{year}年{month}月'


def _get_current_month_dates() -> tuple[list[str], str]:
    today = datetime.now(JST).date()
    return _get_month_dates(today.year, today.month)


def _get_last_month_dates() -> tuple[list[str], str]:
    today = datetime.now(JST)
    if today.month == 1:
        year, month = today.year - 1, 12
    else:
        year, month = today.year, today.month - 1
    return _get_month_dates(year, month)


def _build_range_report_text(name: str, date_strs: list[str], label: str) -> str:
    records      = get_reports_by_date_range(date_strs)
    user_records = [r for r in records if r.get('ユーザー名') == name]

    lines = [f'{name}さんの日報', label, '']

    if not user_records:
        lines.append('この期間のデータはありません。')
        return '\n'.join(lines)

    by_date: dict[str, dict] = {}
    for rec in user_records:
        d    = rec.get('日付', '')
        slot = rec.get('午前or午後', '')
        if d not in by_date:
            by_date[d] = {}
        by_date[d][slot] = rec

    for date_str in date_strs:
        if date_str not in by_date:
            continue
        slots      = by_date[date_str]
        parts      = date_str.split('/')
        d_lbl      = f'{int(parts[1])}/{int(parts[2])}'
        slot_texts = [
            f'{slot}:{format_action_label(slots[slot])}'
            for slot in ['午前', '午後'] if slot in slots
        ]
        lines.append(f'{d_lbl} {" ".join(slot_texts)}')

    return '\n'.join(lines)


def _reply_range_or_select(reply_token: str, user_id: str, date_strs: list[str], label: str):
    if not date_strs:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(ReplyMessageRequest(
                reply_token=reply_token,
                messages=[V3TextMessage(text='対象日付が取得できませんでした。')],
            ))
        return

    start_str = date_strs[0].replace('/', '-')
    end_str   = date_strs[-1].replace('/', '-')

    if is_admin(user_id):
        users = get_all_users()
        items = [
            QuickReplyItem(action=PostbackAction(
                label='全員',
                data=f'action=view_range_all&start={start_str}&end={end_str}',
                display_text='全員',
            ))
        ]
        for u in users:
            items.append(QuickReplyItem(action=PostbackAction(
                label=u['name'],
                data=f'action=view_range_user&start={start_str}&end={end_str}&name={u["name"]}',
                display_text=u['name'],
            )))
        quick_reply = QuickReply(items=items)
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(ReplyMessageRequest(
                reply_token=reply_token,
                messages=[V3TextMessage(
                    text=f'{label}の日報を確認するユーザーを選んでください',
                    quick_reply=quick_reply,
                )],
            ))
    else:
        display_name = get_display_name(user_id)
        report_text  = _build_range_report_text(display_name, date_strs, label)
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(ReplyMessageRequest(
                reply_token=reply_token,
                messages=[V3TextMessage(text=report_text)],
            ))


# ─────────────────────────────────────
# Flask ルート
# ─────────────────────────────────────

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    reply = ask_claude(event.message.text)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


@app.route('/morning_report', methods=['GET'])
def run_morning_report():
    def run():
        try:
            from morning_report import morning_report
            morning_report()
        except Exception as e:
            logger.error(f'morning_report エラー: {e}')
    threading.Thread(target=run).start()
    return 'OK', 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
