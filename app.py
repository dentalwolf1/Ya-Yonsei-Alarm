import os
import re
import json
import hmac
import hashlib
import logging
from datetime import datetime, timedelta

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

scheduler = BackgroundScheduler(
    jobstores={"default": MemoryJobStore()},
    timezone=KST,
)
if not scheduler.running:
    scheduler.start()
    logger.info("Scheduler started (KST)")

# ---------------------------------------------------------------------------
# Phone number storage — persists across Render restarts via JSON file
# ---------------------------------------------------------------------------
PHONE_BOOK_FILE = os.path.join(os.path.dirname(__file__), "phone_book.json")


def _load_phone_book() -> dict:
    """Load phone book from disk. Returns empty dict if file doesn't exist."""
    try:
        with open(PHONE_BOOK_FILE, "r") as f:
            data = json.load(f)
            logger.info("Phone book loaded: %d users", len(data))
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        logger.info("No existing phone book found — starting fresh")
        return {}


def _save_phone_book() -> None:
    """Save phone book to disk immediately after any change."""
    try:
        with open(PHONE_BOOK_FILE, "w") as f:
            json.dump(user_phone_book, f)
        logger.info("Phone book saved: %d users", len(user_phone_book))
    except Exception as e:
        logger.error("Failed to save phone book: %s", e)


user_phone_book: dict = _load_phone_book()


# ---------------------------------------------------------------------------
# Kakao Open Builder response helper
# ---------------------------------------------------------------------------

def send_text_response(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}]
        }
    }


# ---------------------------------------------------------------------------
# Solapi SMS
# ---------------------------------------------------------------------------

def _solapi_auth_header() -> dict:
    now     = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    salt    = os.urandom(16).hex()
    message = now + salt
    sig     = hmac.new(
        SOLAPI_API_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return {
        "Authorization": (
            f"HMAC-SHA256 apiKey={SOLAPI_API_KEY}, "
            f"date={now}, salt={salt}, signature={sig}"
        ),
        "Content-Type": "application/json",
    }


def send_sms(phone_number: str, text: str) -> None:
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
    logger.info("Sending SMS to %s", phone_number)
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    if resp.status_code not in (200, 201):
        logger.error("SMS FAILED | phone: %s | status: %s | response: %s",
                     phone_number, resp.status_code, resp.text)
    else:
        logger.info("SMS SENT | phone: %s | time: %s KST",
                    phone_number,
                    datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S"))


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def schedule_reminders(phone_number: str, event_dt: datetime, title: str) -> list:
    now_kst   = datetime.now(tz=KST)
    scheduled = []

    for minutes_before in (10, 5):
        fire_at = event_dt - timedelta(minutes=minutes_before)
        if fire_at <= now_kst:
            logger.info("Skipping %d-min reminder — already past", minutes_before)
            continue

        job_id  = f"{phone_number}_{title}_{minutes_before}min"
        message = (
            f"[YA! Yonsei Alarm]\n"
            f"⏰ '{title}' 이(가) {minutes_before}분 후 시작됩니다!"
        )

        scheduler.add_job(
            send_sms,
            trigger="date",
            run_date=fire_at,
            args=[phone_number, message],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=120,
        )
        scheduled.append(fire_at.strftime("%H:%M"))
        logger.info("Alarm scheduled | '%s' | %d-min at %s KST",
                    title, minutes_before, fire_at.strftime("%Y-%m-%d %H:%M %Z"))

    return scheduled


# ---------------------------------------------------------------------------
# Input parsers
# ---------------------------------------------------------------------------

# First-time registration: 01012345678
PHONE_ONLY_PATTERN = re.compile(r'^(01\d{8,9})\s*$')

# With phone: 01012345678 06/10/14:30 "title"
WITH_PHONE_PATTERN = re.compile(
    r'^(01\d{8,9})\s+(\d{1,2})/(\d{1,2})/(\d{1,2}):(\d{2})\s+"?(.+?)"?\s*$'
)

# Without phone (returning user): 06/10/14:30 "title" or 06/10/14:30 title
NO_PHONE_PATTERN = re.compile(
    r'^(\d{1,2})/(\d{1,2})/(\d{1,2}):(\d{2})\s+"?(.+?)"?\s*$',
    re.UNICODE
)

REGISTER_HELP = (
    "처음 사용하시는군요! 📱\n"
    "먼저 전화번호를 등록해 주세요.\n\n"
    "전화번호만 입력:\n"
    "예시: 01012345678\n\n"
    "또는 전화번호와 함께 바로 알림 설정:\n"
    "예시: 01012345678 06/10/14:30 \"팀 미팅\""
)

FORMAT_HELP = (
    "입력 형식: 월/일/시:분 \"알림 제목\"\n"
    "예시: 06/10/14:30 \"팀 미팅\"\n"
    "예시: 06/05/09:00 \"셔틀버스\"\n"
    "• 시간은 24시간 형식 (한국 표준시 기준)\n"
    "• SMS로 알림이 발송됩니다"
)


def parse_datetime(month, day, hour, minute):
    """Build a KST-aware datetime, rolling to next year if in the past."""
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


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    body         = request.get_json(silent=True) or {}
    user_request = body.get("userRequest", {})
    # Try multiple paths Kakao uses for user ID
    user_obj = user_request.get("user", {})
    user_id  = (
        user_obj.get("id") or
        user_obj.get("userId") or
        user_obj.get("key") or
        body.get("bot", {}).get("id", "")
    )
    user_id = str(user_id).strip()
    logger.info("Resolved user_id: '%s'", user_id)
    utterance    = user_request.get("utterance", "").strip()

    logger.info("Webhook | user: %s | utterance: %s", user_id, utterance)

    if not utterance:
        return jsonify(send_text_response(FORMAT_HELP))

    # ── Case 1: user sends phone number only → register it ──────────────────
    phone_only = PHONE_ONLY_PATTERN.match(utterance)
    if phone_only:
        phone = phone_only.group(1)
        user_phone_book[user_id] = phone
        _save_phone_book()
        logger.info("Registered phone %s for user %s", phone, user_id)
        return jsonify(send_text_response(
            f"✅ 전화번호 {phone} 가 등록되었습니다!\n\n"
            f"이제 아래 형식으로 알림을 설정하세요:\n"
            f"월/일/시:분 \"알림 제목\"\n"
            f"예시: 06/10/14:30 \"팀 미팅\""
        ))

    # ── Case 2: user sends phone + datetime + title (first time with alarm) ──
    with_phone = WITH_PHONE_PATTERN.match(utterance)
    if with_phone:
        phone  = with_phone.group(1)
        month  = int(with_phone.group(2))
        day    = int(with_phone.group(3))
        hour   = int(with_phone.group(4))
        minute = int(with_phone.group(5))
        title  = with_phone.group(6).strip().strip('"').strip("'")

        user_phone_book[user_id] = phone   # save for future use
        _save_phone_book()
        logger.info("Registered phone %s for user %s", phone, user_id)

        event_dt = parse_datetime(month, day, hour, minute)
        if event_dt is None:
            return jsonify(send_text_response(
                "⚠️ 날짜/시간이 올바르지 않습니다.\n" + FORMAT_HELP
            ))

        scheduled_times = schedule_reminders(phone, event_dt, title)
        return _confirmation_response(title, event_dt, scheduled_times, phone)

    # ── Case 3: returning user sends datetime + title only ───────────────────
    no_phone = NO_PHONE_PATTERN.match(utterance)
    if no_phone:
        if not user_id or user_id not in user_phone_book:
            logger.info("No phone found for user_id: '%s' | phonebook keys: %s", user_id, list(user_phone_book.keys()))
            # Always show registration prompt — never a format error
            return jsonify(send_text_response(REGISTER_HELP))

        phone  = user_phone_book[user_id]
        month  = int(no_phone.group(1))
        day    = int(no_phone.group(2))
        hour   = int(no_phone.group(3))
        minute = int(no_phone.group(4))
        title  = no_phone.group(5).strip().strip('"').strip("'")

        event_dt = parse_datetime(month, day, hour, minute)
        if event_dt is None:
            return jsonify(send_text_response(
                "⚠️ 날짜/시간이 올바르지 않습니다.\n" + FORMAT_HELP
            ))

        scheduled_times = schedule_reminders(phone, event_dt, title)
        return _confirmation_response(title, event_dt, scheduled_times, phone)

    # ── No pattern matched ───────────────────────────────────────────────────
    # If user has no phone registered, always show registration prompt
    # regardless of what they typed — never show a confusing format error
    phone_on_file = user_id and user_id in user_phone_book
    if phone_on_file:
        return jsonify(send_text_response(
            "⚠️ 입력 형식이 올바르지 않습니다.\n\n" + FORMAT_HELP
        ))
    else:
        return jsonify(send_text_response(REGISTER_HELP))


def _confirmation_response(title, event_dt, scheduled_times, phone):
    event_str = event_dt.strftime("%m월 %d일 %H:%M (한국 표준시)")
    if not scheduled_times:
        return jsonify(send_text_response(
            f"⚠️ '{title}' 알림을 설정할 수 없습니다.\n"
            f"이벤트 시간({event_str})이 너무 가깝습니다.\n"
            "최소 11분 이후의 일정을 입력해 주세요."
        ))
    times_str = " / ".join(f"{t} KST" for t in scheduled_times)
    return jsonify(send_text_response(
        f"✅ 알림이 설정되었습니다!\n"
        f"📅 제목: {title}\n"
        f"🕐 이벤트: {event_str}\n"
        f"📱 SMS 수신: {phone}\n"
        f"🔔 알림 시간: {times_str}\n"
        f"(이벤트 10분 전, 5분 전 문자 발송)"
    ))


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    now_kst = datetime.now(tz=KST)
    jobs = [
        {
            "id": j.id,
            "next_run_kst": j.next_run_time.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
            if j.next_run_time else None
        }
        for j in scheduler.get_jobs()
    ]
    return jsonify({
        "status": "ok",
        "server_time_kst": now_kst.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "sms_ready": bool(SOLAPI_API_KEY and SOLAPI_API_SECRET and SOLAPI_SENDER),
        "registered_users": len(user_phone_book),
        "scheduled_jobs_count": len(jobs),
        "scheduled_jobs": jobs,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
