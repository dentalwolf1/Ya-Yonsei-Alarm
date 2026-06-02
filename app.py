import os
import re
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

KAKAO_CHANNEL_ACCESS_TOKEN = os.getenv("KAKAO_CHANNEL_ACCESS_TOKEN", "")
if not KAKAO_CHANNEL_ACCESS_TOKEN:
    logger.warning(
        "KAKAO_CHANNEL_ACCESS_TOKEN is not set — push messages will fail. "
        "Add it in the Render dashboard under Environment Variables."
    )

# ---------------------------------------------------------------------------
# Timezone — ALWAYS Korean Standard Time (UTC+9)
# Server may be in Oregon but all times are anchored to KST.
# ---------------------------------------------------------------------------
KST = pytz.timezone("Asia/Seoul")
TIMEZONE = KST

scheduler = BackgroundScheduler(
    jobstores={"default": MemoryJobStore()},
    timezone=KST,
)
if not scheduler.running:
    scheduler.start()


# ---------------------------------------------------------------------------
# Kakao API helpers
# ---------------------------------------------------------------------------

def send_text_response(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}]
        }
    }


def send_push_message(user_id: str, text: str) -> None:
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
    """Schedule T-10min and T-5min reminders. event_dt must be KST-aware."""
    now_kst = datetime.now(tz=KST)

    for minutes_before in (10, 5):
        fire_at = event_dt - timedelta(minutes=minutes_before)
        if fire_at <= now_kst:
            logger.info("Skipping %d-min reminder — already past (KST)", minutes_before)
            continue

        job_id  = f"{user_id}_{event_label}_{minutes_before}min"
        message = f"⏰ 알림: '{event_label}' 이(가) {minutes_before}분 후 시작됩니다!"

        scheduler.add_job(
            send_push_message,
            trigger="date",
            run_date=fire_at,
            args=[user_id, message],
            id=job_id,
            replace_existing=True,
        )
        logger.info(
            "Scheduled %d-min reminder at %s KST for user %s",
            minutes_before,
            fire_at.strftime("%Y-%m-%d %H:%M %Z"),
            user_id,
        )


# ---------------------------------------------------------------------------
# Parser — expects: MM/DD/HH:MM "title"
# Examples:
#   06/10/14:30 "팀 미팅"
#   6/10/09:00 "셔틀버스"
#   12/31/23:59 "새해 카운트다운"
# ---------------------------------------------------------------------------

FORMAT_HELP = (
    "입력 형식: 월/일/시간 \"알림 제목\"\n"
    "예시: 06/10/14:30 \"팀 미팅\"\n"
    "예시: 6/10/09:00 \"셔틀버스\"\n"
    "• 시간은 24시간 형식으로 입력해 주세요 (한국 표준시 기준)"
)

# Matches: M/D/HH:MM "title" or M/D/HH:MM title (quotes optional)
INPUT_PATTERN = re.compile(
    r'^(\d{1,2})/(\d{1,2})/(\d{1,2}):(\d{2})\s+"?(.+?)"?\s*$'
)


def parse_event_datetime(utterance: str):
    """
    Parse input in the format: MM/DD/HH:MM "title"
    Returns (datetime_kst, title) or (None, None) on failure.
    All times are treated as Korean Standard Time (KST, UTC+9).
    """
    match = INPUT_PATTERN.match(utterance.strip())
    if not match:
        return None, None

    month  = int(match.group(1))
    day    = int(match.group(2))
    hour   = int(match.group(3))
    minute = int(match.group(4))
    title  = match.group(5).strip().strip('"')

    now_kst = datetime.now(tz=KST)
    year    = now_kst.year

    try:
        dt = KST.localize(datetime(year, month, day, hour, minute))
    except ValueError:
        return None, None

    # If the date has already passed this year, use next year
    if dt <= now_kst:
        try:
            dt = KST.localize(datetime(year + 1, month, day, hour, minute))
        except ValueError:
            return None, None

    return dt, title


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    body        = request.get_json(silent=True) or {}
    user_request = body.get("userRequest", {})
    user_id     = user_request.get("user", {}).get("id", "")
    utterance   = user_request.get("utterance", "").strip()

    logger.info("Incoming utterance: %s", utterance)

    if not utterance:
        return jsonify(send_text_response(FORMAT_HELP))

    event_dt, title = parse_event_datetime(utterance)

    if event_dt is None or title is None:
        return jsonify(send_text_response(
            "입력 형식이 올바르지 않습니다.\n\n" + FORMAT_HELP
        ))

    schedule_reminders(user_id, event_dt, title)

    formatted = event_dt.strftime("%Y년 %m월 %d일 %H:%M (한국 표준시)")
    return jsonify(send_text_response(
        f"✅ 알림이 설정되었습니다!\n"
        f"📅 제목: {title}\n"
        f"🕐 일시: {formatted}\n"
        f"🔔 10분 전과 5분 전에 알림을 보내드립니다."
    ))


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    now_kst = datetime.now(tz=KST)
    jobs = [{"id": j.id, "next_run_kst": str(j.next_run_time)} for j in scheduler.get_jobs()]
    return jsonify({
        "status": "ok",
        "server_time_kst": now_kst.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "scheduled_jobs": jobs,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
