import os
import re
import json
import hmac
import hashlib
import logging
import threading
import time as _time
from datetime import datetime, timedelta, time, date

import pytz
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Solapi credentials
# ---------------------------------------------------------------------------
SOLAPI_API_KEY    = os.getenv("SOLAPI_API_KEY", "")
SOLAPI_API_SECRET = os.getenv("SOLAPI_API_SECRET", "")
SOLAPI_SENDER     = os.getenv("SOLAPI_SENDER", "")

if not SOLAPI_API_KEY or not SOLAPI_API_SECRET or not SOLAPI_SENDER:
    logger.warning("Solapi credentials not fully set — SMS will fail.")
else:
    logger.info("Solapi SMS credentials loaded successfully")

# ---------------------------------------------------------------------------
# Timezone — ALWAYS Korean Standard Time (UTC+9)
# ---------------------------------------------------------------------------
KST = pytz.timezone("Asia/Seoul")

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(
    jobstores={"default": MemoryJobStore()},
    timezone=KST,
)
if not scheduler.running:
    scheduler.start()
    logger.info("Scheduler started (KST)")

# ---------------------------------------------------------------------------
# Keep-alive — pings server every 10 min to prevent Render free tier sleep.
# ---------------------------------------------------------------------------
APP_URL = os.getenv("APP_URL", "https://ya-yonsei-alarm.onrender.com")


def _keep_alive():
    while True:
        _time.sleep(600)
        try:
            requests.get(f"{APP_URL}/health", timeout=10)
            logger.info("Keep-alive ping sent to %s", APP_URL)
        except Exception as e:
            logger.warning("Keep-alive ping failed: %s", e)


threading.Thread(target=_keep_alive, daemon=True).start()
logger.info("Keep-alive thread started")

# ---------------------------------------------------------------------------
# Phone book — persists across restarts via JSON file
# ---------------------------------------------------------------------------
PHONE_BOOK_FILE = os.path.join(os.path.dirname(__file__), "phone_book.json")


def _load_phone_book() -> dict:
    try:
        with open(PHONE_BOOK_FILE, "r") as f:
            data = json.load(f)
            logger.info("Phone book loaded: %d users", len(data))
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        logger.info("No existing phone book — starting fresh")
        return {}


def _save_phone_book() -> None:
    try:
        with open(PHONE_BOOK_FILE, "w") as f:
            json.dump(user_phone_book, f)
        logger.info("Phone book saved: %d users", len(user_phone_book))
    except Exception as e:
        logger.error("Failed to save phone book: %s", e)


user_phone_book: dict = _load_phone_book()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def send_text_response(text: str) -> dict:
    """Immediate response back to user in KakaoTalk chat."""
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]},
    }


def get_user_id(body: dict) -> str:
    """Extract user ID from Kakao webhook body."""
    user_request = body.get("userRequest", {})
    user_obj     = user_request.get("user", {})
    user_id      = (
        user_obj.get("id") or
        user_obj.get("userId") or
        user_obj.get("key") or
        body.get("bot", {}).get("id", "")
    )
    return str(user_id).strip()


def register_phone(user_id: str, phone: str) -> None:
    """Save phone number for a user permanently."""
    user_phone_book[user_id] = phone
    _save_phone_book()
    logger.info("Registered phone %s for user %s", phone, user_id)


PHONE_ONLY_PATTERN = re.compile(r'^(01\d{8,9})\s*$')

REGISTER_HELP_ALARM = (
    "처음 사용하시는군요! 📱\n"
    "아래 형식으로 알림을 설정하세요:\n\n"
    "📌 형식:\n"
    "알림 전화번호 MM/DD HH:MM \"제목\"\n"
    "알림 전화번호 MM/DD \"제목\" (시간 생략 시 09:00)\n\n"
    "📌 예시:\n"
    "알림 01012345678 06/10 13:00 \"치사회시험\"\n"
    "알림 01012345678 06/07 \"테스트\""
)

REGISTER_HELP_SHUTTLE = (
    "처음 사용하시는군요! 📱\n"
    "먼저 전화번호를 한 번만 등록해 주세요.\n\n"
    "예시: 01012345678\n\n"
    "등록 후에는 날짜만 입력하시면 됩니다!\n"
    "예시: 06/10"
)


# ---------------------------------------------------------------------------
# Solapi SMS sender
# ---------------------------------------------------------------------------

def _solapi_auth_header() -> dict:
    now     = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    salt    = os.urandom(16).hex()
    message = now + salt
    sig     = hmac.new(
        SOLAPI_API_SECRET.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": (
            f"HMAC-SHA256 apiKey={SOLAPI_API_KEY}, "
            f"date={now}, salt={salt}, signature={sig}"
        ),
        "Content-Type": "application/json",
    }


def send_sms(phone_number: str, text: str) -> bool:
    """
    Send an SMS via Solapi. Returns True on success, False on failure.
    Logs full response details for debugging.
    """
    url     = "https://api.solapi.com/messages/v4/send"
    headers = _solapi_auth_header()
    payload = {
        "message": {
            "to":   phone_number,
            "from": SOLAPI_SENDER,
            "text": text,
            "type": "SMS",
        }
    }

    logger.info(
        "SMS REQUEST | to: %s | from: %s | text: %s",
        phone_number, SOLAPI_SENDER, text
    )

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        now_kst = datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S")

        if resp.status_code in (200, 201):
            logger.info(
                "SMS SENT ✅ | to: %s | time: %s KST | response: %s",
                phone_number, now_kst, resp.text
            )
            return True
        else:
            logger.error(
                "SMS FAILED ❌ | to: %s | status: %s | response: %s",
                phone_number, resp.status_code, resp.text
            )
            return False

    except Exception as e:
        logger.error("SMS EXCEPTION | to: %s | error: %s", phone_number, e)
        return False


# ===========================================================================
# FEATURE 1 — GENERAL ALARM  (/webhook/alarm)
#
# Input format (matching the screenshot patterns):
#   알림 01012345678 06/09 13:00 "치사회시험"   ← with time
#   알림 01012345678 06/06 21:51 "Alarm check"  ← with time
#   알림 01012345678 06/07 테스트               ← no quotes, no time (09:00)
#   알림 01012345678 06/07 09:00 "테스트"       ← with time
#   알림 01012345678 06/07 "테스트"             ← no time (09:00)
#
# Returning users (phone already registered):
#   06/10 14:30 "팀 미팅"
#   06/10 "팀 미팅"
# ===========================================================================

ALARM_FORMAT_HELP = (
    "📌 입력 형식:\n"
    "알림 전화번호 MM/DD HH:MM \"제목\"\n"
    "알림 전화번호 MM/DD \"제목\" (시간 생략 시 09:00)\n\n"
    "📌 예시:\n"
    "알림 01012345678 06/10 13:00 \"치사회시험\"\n"
    "알림 01012345678 06/07 \"테스트\"\n\n"
    "• 시간은 24시간 형식 (한국 표준시 기준)\n"
    "• SMS로 이벤트 10분 전, 5분 전 알림 발송"
)

# ── Patterns for "알림 ..." prefix inputs ──────────────────────────────────

# 알림 01012345678 06/09 13:00 "치사회시험"   (with time, with/without quotes)
ALARM_WITH_PHONE_WITH_TIME = re.compile(
    r'^알림\s+(01\d{8,9})\s+(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*"?(.+?)"?\s*$',
    re.UNICODE
)
# 알림 01012345678 06/07 "테스트"  or  알림 01012345678 06/07 테스트  (no time → 09:00)
ALARM_WITH_PHONE_NO_TIME = re.compile(
    r'^알림\s+(01\d{8,9})\s+(\d{1,2})/(\d{1,2})\s+"?(.+?)"?\s*$',
    re.UNICODE
)

# ── Patterns for returning users (no "알림" prefix, no phone) ──────────────

# 06/10 14:30 "팀 미팅"
ALARM_NO_PHONE_WITH_TIME = re.compile(
    r'^(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*"?(.+?)"?\s*$',
    re.UNICODE
)
# 06/10 "팀 미팅"  or  06/10 팀 미팅  (no time → 09:00)
ALARM_NO_PHONE_NO_TIME = re.compile(
    r'^(\d{1,2})/(\d{1,2})\s+"?(.+?)"?\s*$',
    re.UNICODE
)


def parse_alarm_datetime(month: int, day: int, hour: int = 9, minute: int = 0) -> datetime | None:
    now_kst = datetime.now(tz=KST)
    year    = now_kst.year
    try:
        dt = KST.localize(datetime(year, month, day, hour, minute))
    except ValueError:
        return None
    if dt <= now_kst:
        try:
            dt = KST.localize(datetime(year + 1, month, day, hour, minute))
        except ValueError:
            return None
    return dt


def schedule_alarm_reminders(phone: str, event_dt: datetime, title: str) -> list:
    now_kst   = datetime.now(tz=KST)
    scheduled = []
    for minutes_before in (10, 5):
        fire_at = event_dt - timedelta(minutes=minutes_before)
        if fire_at <= now_kst:
            continue
        job_id  = f"alarm_{phone}_{title}_{minutes_before}min"
        message = f"[YA! 연세 알람]\n⏰ '{title}' 이(가) {minutes_before}분 후 시작됩니다!"
        scheduler.add_job(
            send_sms, trigger="date", run_date=fire_at,
            args=[phone, message], id=job_id,
            replace_existing=True, misfire_grace_time=120,
        )
        scheduled.append(fire_at.strftime("%H:%M"))
        logger.info(
            "Alarm scheduled | '%s' | %d-min reminder at %s KST",
            title, minutes_before, fire_at.strftime("%Y-%m-%d %H:%M")
        )
    return scheduled


@app.route("/webhook/alarm", methods=["POST"])
def webhook_alarm():
    body      = request.get_json(silent=True) or {}
    user_id   = get_user_id(body)
    utterance = body.get("userRequest", {}).get("utterance", "").strip()

    logger.info("ALARM webhook | user: %s | utterance: %s", user_id, utterance)

    if not utterance:
        return jsonify(send_text_response(ALARM_FORMAT_HELP))

    # ── "알림 phone date time title" ──────────────────────────────────────
    m = ALARM_WITH_PHONE_WITH_TIME.match(utterance)
    if m:
        phone = m.group(1)
        register_phone(user_id, phone)
        event_dt = parse_alarm_datetime(
            int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5))
        )
        title = m.group(6).strip().strip('"')
        if event_dt is None:
            return jsonify(send_text_response("⚠️ 날짜/시간이 올바르지 않습니다.\n\n" + ALARM_FORMAT_HELP))
        # Send immediate confirmation SMS
        _send_confirmation_sms(phone, title, event_dt)
        times = schedule_alarm_reminders(phone, event_dt, title)
        return _alarm_confirmation(title, event_dt, times, phone)

    # ── "알림 phone date title" (no time → 09:00) ─────────────────────────
    m = ALARM_WITH_PHONE_NO_TIME.match(utterance)
    if m:
        phone = m.group(1)
        register_phone(user_id, phone)
        event_dt = parse_alarm_datetime(int(m.group(2)), int(m.group(3)))  # defaults 09:00
        title = m.group(4).strip().strip('"')
        if event_dt is None:
            return jsonify(send_text_response("⚠️ 날짜가 올바르지 않습니다.\n\n" + ALARM_FORMAT_HELP))
        _send_confirmation_sms(phone, title, event_dt)
        times = schedule_alarm_reminders(phone, event_dt, title)
        return _alarm_confirmation(title, event_dt, times, phone)

    # ── Returning user: "date time title" ────────────────────────────────
    m = ALARM_NO_PHONE_WITH_TIME.match(utterance)
    if m:
        if not user_id or user_id not in user_phone_book:
            return jsonify(send_text_response(REGISTER_HELP_ALARM))
        phone    = user_phone_book[user_id]
        event_dt = parse_alarm_datetime(
            int(m.group(1)), int(m.group(2)),
            int(m.group(3)), int(m.group(4))
        )
        title = m.group(5).strip().strip('"')
        if event_dt is None:
            return jsonify(send_text_response("⚠️ 날짜/시간이 올바르지 않습니다.\n\n" + ALARM_FORMAT_HELP))
        _send_confirmation_sms(phone, title, event_dt)
        times = schedule_alarm_reminders(phone, event_dt, title)
        return _alarm_confirmation(title, event_dt, times, phone)

    # ── Returning user: "date title" (no time → 09:00) ────────────────────
    m = ALARM_NO_PHONE_NO_TIME.match(utterance)
    if m:
        if not user_id or user_id not in user_phone_book:
            return jsonify(send_text_response(REGISTER_HELP_ALARM))
        phone    = user_phone_book[user_id]
        event_dt = parse_alarm_datetime(int(m.group(1)), int(m.group(2)))
        title    = m.group(3).strip().strip('"')
        if event_dt is None:
            return jsonify(send_text_response("⚠️ 날짜가 올바르지 않습니다.\n\n" + ALARM_FORMAT_HELP))
        _send_confirmation_sms(phone, title, event_dt)
        times = schedule_alarm_reminders(phone, event_dt, title)
        return _alarm_confirmation(title, event_dt, times, phone)

    # ── No match ──────────────────────────────────────────────────────────
    return jsonify(send_text_response(
        "⚠️ 입력 형식이 올바르지 않습니다.\n\n" + ALARM_FORMAT_HELP
    ))


def _send_confirmation_sms(phone: str, title: str, event_dt: datetime) -> None:
    """Send an immediate SMS confirming the alarm was registered."""
    event_str = event_dt.strftime("%m월 %d일 %H:%M")
    message = (
        f"[YA! 연세 알람] ✅ 알림 등록 완료\n"
        f"📅 제목: {title}\n"
        f"🕐 일정: {event_str} (KST)\n"
        f"🔔 10분 전, 5분 전 SMS 발송 예정"
    )
    threading.Thread(target=send_sms, args=(phone, message), daemon=True).start()
    logger.info("Confirmation SMS dispatched | to: %s | title: %s", phone, title)


def _alarm_confirmation(title, event_dt, times, phone):
    event_str = event_dt.strftime("%m월 %d일 %H:%M (KST)")
    if not times:
        return jsonify(send_text_response(
            f"⚠️ '{title}' 알림을 설정할 수 없습니다.\n"
            f"이벤트 시간({event_str})이 너무 가깝습니다.\n"
            "최소 11분 이후의 일정을 입력해 주세요."
        ))
    times_str = " / ".join(f"{t} KST" for t in times)
    return jsonify(send_text_response(
        f"✅ 알림이 설정되었습니다!\n"
        f"📅 제목: {title}\n"
        f"🕐 이벤트: {event_str}\n"
        f"📱 SMS 수신: {phone}\n"
        f"🔔 알림 시간: {times_str}\n"
        f"(이벤트 10분 전, 5분 전 문자 발송)\n"
        f"📨 등록 확인 SMS도 발송되었습니다!"
    ))


# ===========================================================================
# FEATURE 2 — SHUTTLE ALARM  (/webhook/shuttle)
# User input: MM/DD  or  M월D일
# ===========================================================================

SHUTTLE_FORMAT_HELP = (
    "셔틀 탑승 날짜를 입력해 주세요.\n\n"
    "입력 형식: 월/일\n"
    "예시: 06/10\n"
    "예시: 6월10일\n\n"
    "• 탑승일 2일 전 13:50, 13:55에 예약 알림 SMS 발송"
)

# With phone + date: 01012345678 06/10
SHUTTLE_WITH_PHONE = re.compile(
    r'^(01\d{8,9})\s+(\d{1,2})[/월]\s*(\d{1,2})일?\s*$'
)
# Date only: 06/10 or 6월10일
SHUTTLE_NO_PHONE = re.compile(
    r'^(\d{1,2})[/월]\s*(\d{1,2})일?\s*$'
)

# Shuttle booking opens at 14:00
SHUTTLE_BOOKING_OPEN = time(14, 0)


def parse_shuttle_date(month: int, day: int) -> date | None:
    now_kst = datetime.now(tz=KST).date()
    year    = now_kst.year
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if d <= now_kst:
        try:
            d = date(year + 1, month, day)
        except ValueError:
            return None
    return d


def schedule_shuttle_alerts(phone: str, intended_date: date) -> list:
    now_kst    = datetime.now(tz=KST)
    alert_date = intended_date - timedelta(days=2)
    date_label = intended_date.strftime("%m월 %d일")
    scheduled  = []

    jobs = [
        (
            f"shuttle_{phone}_{intended_date}_1350",
            KST.localize(datetime.combine(alert_date, time(13, 50))),
            f"[YA! 연세 알람] {date_label} 셔틀 예약이 10분 후 시작됩니다!",
            "13:50 알림",
        ),
        (
            f"shuttle_{phone}_{intended_date}_1355",
            KST.localize(datetime.combine(alert_date, time(13, 55))),
            f"[YA! 연세 알람] {date_label} 셔틀 예약이 5분 후 시작됩니다!",
            "13:55 알림",
        ),
    ]

    for job_id, fire_at, message, label in jobs:
        if fire_at <= now_kst:
            logger.info("Skipping '%s' — already past", label)
            continue
        scheduler.add_job(
            send_sms, trigger="date", run_date=fire_at,
            args=[phone, message], id=job_id,
            replace_existing=True, misfire_grace_time=120,
        )
        scheduled.append((label, fire_at.strftime("%m/%d %H:%M")))
        logger.info("Shuttle alert scheduled | %s | %s KST",
                    label, fire_at.strftime("%Y-%m-%d %H:%M"))

    return scheduled


@app.route("/webhook/shuttle", methods=["POST"])
def webhook_shuttle():
    body      = request.get_json(silent=True) or {}
    user_id   = get_user_id(body)
    utterance = body.get("userRequest", {}).get("utterance", "").strip()

    logger.info("SHUTTLE webhook | user: %s | utterance: %s", user_id, utterance)

    if not utterance:
        return jsonify(send_text_response(SHUTTLE_FORMAT_HELP))

    # Phone only → register
    phone_only = PHONE_ONLY_PATTERN.match(utterance)
    if phone_only:
        phone = phone_only.group(1)
        register_phone(user_id, phone)
        return jsonify(send_text_response(
            f"✅ 전화번호 {phone} 가 등록되었습니다!\n\n"
            "이제 탑승 날짜만 입력하시면 됩니다 😊\n"
            "예시: 06/10"
        ))

    # Phone + date
    m = SHUTTLE_WITH_PHONE.match(utterance)
    if m:
        phone = m.group(1)
        register_phone(user_id, phone)
        intended_date = parse_shuttle_date(int(m.group(2)), int(m.group(3)))
        if intended_date is None:
            return jsonify(send_text_response("⚠️ 날짜가 올바르지 않습니다.\n" + SHUTTLE_FORMAT_HELP))
        scheduled = schedule_shuttle_alerts(phone, intended_date)
        return _shuttle_confirmation(intended_date, scheduled, phone)

    # Date only — returning user
    m = SHUTTLE_NO_PHONE.match(utterance)
    if m:
        if not user_id or user_id not in user_phone_book:
            return jsonify(send_text_response(REGISTER_HELP_SHUTTLE))
        phone = user_phone_book[user_id]
        intended_date = parse_shuttle_date(int(m.group(1)), int(m.group(2)))
        if intended_date is None:
            return jsonify(send_text_response("⚠️ 날짜가 올바르지 않습니다.\n" + SHUTTLE_FORMAT_HELP))
        scheduled = schedule_shuttle_alerts(phone, intended_date)
        return _shuttle_confirmation(intended_date, scheduled, phone)

    # No match
    if user_id and user_id in user_phone_book:
        return jsonify(send_text_response("⚠️ 입력 형식이 올바르지 않습니다.\n\n" + SHUTTLE_FORMAT_HELP))
    return jsonify(send_text_response(REGISTER_HELP_SHUTTLE))


def _shuttle_confirmation(intended_date: date, scheduled: list, phone: str):
    date_str = intended_date.strftime("%m월 %d일")

    if not scheduled:
        return jsonify(send_text_response(
            f"⚠️ {date_str} 셔틀 알림을 설정할 수 없습니다.\n"
            "모든 알림 시간이 이미 지났습니다.\n"
            "더 이후 날짜를 입력해 주세요."
        ))

    lines = "\n".join(f"  • {label}: {t} KST" for label, t in scheduled)
    return jsonify(send_text_response(
        f"✅ 셔틀 알림이 설정되었습니다!\n"
        f"📅 탑승 날짜: {date_str}\n"
        f"📱 SMS 수신: {phone}\n"
        f"🔔 알림 일정:\n{lines}"
    ))


# ===========================================================================
# Health check
# ===========================================================================

@app.route("/health", methods=["GET"])
def health():
    now_kst = datetime.now(tz=KST)
    jobs = [
        {
            "id": j.id,
            "next_run_kst": j.next_run_time.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
            if j.next_run_time else None,
        }
        for j in scheduler.get_jobs()
    ]
    return jsonify({
        "status":               "ok",
        "server_time_kst":      now_kst.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "sms_ready":            bool(SOLAPI_API_KEY and SOLAPI_API_SECRET and SOLAPI_SENDER),
        "registered_users":     len(user_phone_book),
        "scheduled_jobs_count": len(jobs),
        "scheduled_jobs":       jobs,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
