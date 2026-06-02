import os
import logging
from datetime import datetime, timedelta

import pytz
import requests
from dateutil import parser as dateparser
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

KAKAO_CHANNEL_ACCESS_TOKEN = os.getenv("KAKAO_CHANNEL_ACCESS_TOKEN", "")
if not KAKAO_CHANNEL_ACCESS_TOKEN:
    logger.warning(
        "KAKAO_CHANNEL_ACCESS_TOKEN is not set — push messages will fail. "
        "Add it in the Render dashboard under Environment Variables."
    )

TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", "Asia/Seoul"))

# Use a single shared scheduler. gunicorn is started with --workers 1
# (see Procfile) to prevent each worker from creating its own scheduler
# and firing duplicate reminders.
scheduler = BackgroundScheduler(
    jobstores={"default": MemoryJobStore()},
    timezone=TIMEZONE,
)

# Guard against the scheduler being started multiple times (e.g. during
# Flask's reloader or test imports).
if not scheduler.running:
    scheduler.start()


# ---------------------------------------------------------------------------
# Kakao API helpers
# ---------------------------------------------------------------------------

def send_text_response(text: str) -> dict:
    """Return a Kakao i Open Builder skill response payload."""
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text": text}}
            ]
        }
    }


def send_push_message(user_id: str, text: str) -> None:
    """Send a proactive push message via Kakao Channel Message API."""
    url = "https://kapi.kakao.com/v1/api/talk/friends/message/default/send"
    headers = {
        "Authorization": f"Bearer {KAKAO_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    payload = {
        "receiver_uuids": f'["{user_id}"]',
        "template_object": (
            '{"object_type":"text",'
            f'"text":"{text}",'
            '"link":{"web_url":"","mobile_web_url":""}}'
        ),
    }
    resp = requests.post(url, headers=headers, data=payload, timeout=10)
    if resp.status_code != 200:
        logger.error("Push message failed: %s %s", resp.status_code, resp.text)
    else:
        logger.info("Push message sent to %s", user_id)


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------

def schedule_reminders(user_id: str, event_dt: datetime, event_label: str) -> None:
    """Schedule T-10min and T-5min reminder jobs for a user."""
    now = datetime.now(tz=TIMEZONE)

    for minutes_before in (10, 5):
        fire_at = event_dt - timedelta(minutes=minutes_before)
        if fire_at <= now:
            logger.info("Skipping %d-min reminder — already past", minutes_before)
            continue

        job_id = f"{user_id}_{event_label}_{minutes_before}min"
        message = f"⏰ 알림: '{event_label}' 이벤트가 {minutes_before}분 후 시작됩니다!"

        scheduler.add_job(
            send_push_message,
            trigger="date",
            run_date=fire_at,
            args=[user_id, message],
            id=job_id,
            replace_existing=True,
        )
        logger.info("Scheduled %d-min reminder at %s for user %s", minutes_before, fire_at, user_id)


def normalize_korean_datetime(text: str) -> str:
    """
    Convert Korean datetime expressions into a format dateparser understands.
    Handles patterns like:
      오전/오후 + 시/분         → AM/PM hour/minute
      N월 N일                  → month/day
      내일/모레/오늘            → tomorrow/day after tomorrow/today
      N시간 후                 → in N hours
      N분 후                   → in N minutes
    """
    import re

    t = text

    # 내일 → tomorrow, 모레 → day after tomorrow, 오늘 → today
    t = re.sub(r'내일', 'tomorrow', t)
    t = re.sub(r'모레', 'day after tomorrow', t)
    t = re.sub(r'오늘', 'today', t)

    # N시간 후 → in N hours
    t = re.sub(r'(\d+)\s*시간\s*후', r'in \1 hours', t)
    # N분 후 → in N minutes
    t = re.sub(r'(\d+)\s*분\s*후', r'in \1 minutes', t)

    # 오후 N시 → N PM  /  오전 N시 → N AM
    t = re.sub(r'오후\s*(\d+)\s*시\s*(\d+)\s*분', r'\1:\2 PM', t)
    t = re.sub(r'오후\s*(\d+)\s*시', r'\1 PM', t)
    t = re.sub(r'오전\s*(\d+)\s*시\s*(\d+)\s*분', r'\1:\2 AM', t)
    t = re.sub(r'오전\s*(\d+)\s*시', r'\1 AM', t)

    # N월 N일 → month/day
    t = re.sub(r'(\d+)\s*월\s*(\d+)\s*일', r'\1/\2', t)

    # Bare N시 N분 (no 오전/오후) → N:M  (leave AM/PM to dateparser)
    t = re.sub(r'(\d+)\s*시\s*(\d+)\s*분', r'\1:\2', t)
    t = re.sub(r'(\d+)\s*시', r'\1:00', t)

    # Strip leftover Korean particles / words that confuse the parser
    t = re.sub(r'[에|의|은|는|이|가|을|를|로|으로|에서|까지]', ' ', t)

    return t.strip()


def parse_event_datetime(text: str) -> datetime | None:
    """
    Parse Korean or English free-form datetime text into a KST-aware datetime.
    Supported examples:
      "6월 10일 오후 2시 30분 팀 미팅"
      "내일 오전 9시 스탠드업"
      "2026-06-10 14:30 회의"
      "30분 후 알림"
      "tomorrow 3pm standup"
    Always returns a future datetime; rolls forward if needed.
    """
    try:
        now = datetime.now(tz=TIMEZONE)
        normalized = normalize_korean_datetime(text)
        logger.info("Normalized datetime text: '%s' → '%s'", text, normalized)

        dt = dateparser.parse(
            normalized,
            fuzzy=True,
            dayfirst=False,
            settings={
                "PREFER_DATES_FROM": "future",
                "PREFER_DAY_OF_MONTH": "first",
                "TIMEZONE": "Asia/Seoul",
                "RETURN_AS_TIMEZONE_AWARE": True,
            },
        )

        if dt is None:
            # Last resort: try the original text unchanged
            dt = dateparser.parse(
                text,
                fuzzy=True,
                settings={
                    "PREFER_DATES_FROM": "future",
                    "TIMEZONE": "Asia/Seoul",
                    "RETURN_AS_TIMEZONE_AWARE": True,
                },
            )

        if dt is None:
            return None

        if dt.tzinfo is None:
            dt = TIMEZONE.localize(dt)

        # Ensure result is in the future
        if dt <= now:
            if dt.date() == now.date():
                dt = dt + timedelta(days=1)
            if dt <= now:
                dt = dt.replace(year=dt.year + 1)

        return dt
    except (ValueError, OverflowError) as e:
        logger.warning("parse_event_datetime error: %s", e)
        return None


def extract_label(text: str, dt: datetime) -> str:
    """Use the raw user text minus the date portion as the event label."""
    # Remove the detected datetime string from the text to get a cleaner label
    label = text.strip()
    # Fall back to the formatted datetime if no meaningful label remains
    if not label:
        label = dt.strftime("%Y-%m-%d %H:%M")
    return label


# ---------------------------------------------------------------------------
# Webhook endpoint (called by Kakao i Open Builder)
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_json(silent=True) or {}
    logger.info("Incoming webhook: %s", body)

    # Kakao i Open Builder request structure
    user_request = body.get("userRequest", {})
    user_id = user_request.get("user", {}).get("id", "")
    utterance = user_request.get("utterance", "").strip()

    if not utterance:
        return jsonify(send_text_response("이벤트 날짜와 시간을 입력해 주세요.\n예: 2026-06-10 14:30 팀 미팅"))

    event_dt = parse_event_datetime(utterance)

    if event_dt is None:
        return jsonify(send_text_response(
            "날짜와 시간을 인식하지 못했습니다. 다시 시도해 주세요.\n"
            "예: '2026-06-10 14:30 팀 미팅' 또는 '6월 10일 오후 2시 회의'"
        ))

    now = datetime.now(tz=TIMEZONE)
    if event_dt <= now:
        return jsonify(send_text_response("입력하신 시간이 이미 지났습니다. 미래 일정을 입력해 주세요."))

    label = extract_label(utterance, event_dt)
    schedule_reminders(user_id, event_dt, label)

    formatted = event_dt.strftime("%Y년 %m월 %d일 %H:%M")
    return jsonify(send_text_response(
        f"✅ 알림이 설정되었습니다!\n"
        f"📅 이벤트: {label}\n"
        f"🕐 일시: {formatted}\n"
        f"🔔 {formatted} 10분 전과 5분 전에 알림을 보내드립니다."
    ))


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    jobs = [{"id": j.id, "next_run": str(j.next_run_time)} for j in scheduler.get_jobs()]
    return jsonify({"status": "ok", "scheduled_jobs": jobs})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
