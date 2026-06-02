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


def parse_event_datetime(text: str) -> datetime | None:
    """
    Try to extract a datetime from free-form Korean or English text.
    Examples the user might type:
      "2026-06-10 14:30 회의"
      "6월 10일 오후 2시 30분 팀 미팅"
      "tomorrow 3pm standup"
    Returns a timezone-aware datetime or None if parsing fails.
    """
    try:
        dt = dateparser.parse(text, fuzzy=True, dayfirst=False)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = TIMEZONE.localize(dt)
        return dt
    except (ValueError, OverflowError):
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
