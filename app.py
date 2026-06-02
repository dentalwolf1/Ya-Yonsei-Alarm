import os
import re
import logging
import threading
import time
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
APP_URL = os.getenv("APP_URL", "https://ya-yonsei-alarm.onrender.com")

if not KAKAO_CHANNEL_ACCESS_TOKEN:
    logger.warning(
        "KAKAO_CHANNEL_ACCESS_TOKEN is not set — push messages will fail. "
        "Add it in the Render dashboard under Environment Variables."
    )

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
# Keep-alive — pings the server every 10 minutes so Render never sleeps.
# A sleeping server misses scheduled alarms entirely.
# ---------------------------------------------------------------------------
def _keep_alive_loop():
    while True:
        time.sleep(600)  # 10 minutes
        try:
            requests.get(f"{APP_URL}/health", timeout=10)
            logger.info("Keep-alive ping sent")
        except Exception as e:
            logger.warning("Keep-alive ping failed: %s", e)

threading.Thread(target=_keep_alive_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Kakao API helpers
# ---------------------------------------------------------------------------

def send_text_response(text: str) -> dict:
    """Skill response back to Open Builder (shown immediately in chat)."""
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}]
        }
    }


def send_push_message(user_id: str, text: str) -> None:
    """
    Push a message to the user via Kakao Channel (business) API.
    This fires automatically at alarm time — no user action needed.

    Uses the channel memo API so any user who messaged the channel
    can receive push notifications.
    """
    url = "https://kapi.kakao.com/v1/api/talk/memo/default/send"
    headers = {
        "Authorization": f"Bearer {KAKAO_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    template = (
        '{"object_type":"text",'
        f'"text":"{text}",'
        '"link":{"web_url":"","mobile_web_url":""}}'
    )
    payload = {"template_object": template}

    resp = requests.post(url, headers=headers, data=payload, timeout=10)
    if resp.status_code != 200:
        logger.error(
            "Push message FAILED for user %s: %s %s",
            user_id, resp.status_code, resp.text
        )
    else:
        logger.info(
            "Push message SENT to user %s at %s KST",
            user_id,
            datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S")
        )


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------

def schedule_reminders(user_id: str, event_dt: datetime, title: str) -> list[str]:
    """
    Schedule automatic push notifications at T-10min and T-5min.
    Returns list of scheduled fire times (as strings) for confirmation.
    event_dt must be a KST-aware datetime.
    """
    now_kst   = datetime.now(tz=KST)
    scheduled = []

    for minutes_before in (10, 5):
        fire_at = event_dt - timedelta(minutes=minutes_before)

        if fire_at <= now_kst:
            logger.info(
                "Skipping %d-min reminder for '%s' — fire time %s already past",
                minutes_before, title, fire_at.strftime("%H:%M KST")
            )
            continue

        job_id  = f"{user_id}_{title}_{minutes_before}min"
        message = f"⏰ 알림: '{title}' 이(가) {minutes_before}분 후 시작됩니다!"

        scheduler.add_job(
            send_push_message,
            trigger="date",
            run_date=fire_at,           # KST-aware → scheduler fires correctly
            args=[user_id, message],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=120,     # fire up to 2 min late if server was busy
        )

        scheduled.append(fire_at.strftime("%H:%M"))
        logger.info(
            "Alarm scheduled: '%s' — %d-min reminder at %s KST (job id: %s)",
            title, minutes_before,
            fire_at.strftime("%Y-%m-%d %H:%M %Z"),
            job_id,
        )

    return scheduled


# ---------------------------------------------------------------------------
# Input parser  —  MM/DD/HH:MM "title"
# ---------------------------------------------------------------------------

FORMAT_HELP = (
    "입력 형식: 월/일/시:분 \"알림 제목\"\n"
    "예시: 06/10/14:30 \"팀 미팅\"\n"
    "예시: 06/05/09:00 \"셔틀버스\"\n"
    "• 시간은 24시간 형식 (한국 표준시 기준)"
)

# Accepts both with and without quotes around the title
INPUT_PATTERN = re.compile(
    r'^(\d{1,2})/(\d{1,2})/(\d{1,2}):(\d{2})\s+"?(.+?)"?\s*$'
)


def parse_input(utterance: str):
    """
    Parse 'MM/DD/HH:MM "title"' into (kst_datetime, title).
    Returns (None, None) if the format doesn't match.
    """
    match = INPUT_PATTERN.match(utterance.strip())
    if not match:
        return None, None

    month  = int(match.group(1))
    day    = int(match.group(2))
    hour   = int(match.group(3))
    minute = int(match.group(4))
    title  = match.group(5).strip().strip('"').strip("'")

    now_kst = datetime.now(tz=KST)
    year    = now_kst.year

    try:
        dt = KST.localize(datetime(year, month, day, hour, minute))
    except ValueError:
        logger.warning("Invalid date values: %d/%d %d:%d", month, day, hour, minute)
        return None, None

    # Past date this year → try next year
    if dt <= now_kst:
        try:
            dt = KST.localize(datetime(year + 1, month, day, hour, minute))
        except ValueError:
            return None, None

    return dt, title


# ---------------------------------------------------------------------------
# Webhook — entry point for all Open Builder skill calls
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    body         = request.get_json(silent=True) or {}
    user_request = body.get("userRequest", {})
    user_id      = user_request.get("user", {}).get("id", "")
    utterance    = user_request.get("utterance", "").strip()

    logger.info("Webhook received | user: %s | utterance: %s", user_id, utterance)

    if not utterance:
        return jsonify(send_text_response(FORMAT_HELP))

    event_dt, title = parse_input(utterance)

    if event_dt is None or title is None:
        return jsonify(send_text_response(
            "⚠️ 입력 형식이 올바르지 않습니다.\n\n" + FORMAT_HELP
        ))

    scheduled_times = schedule_reminders(user_id, event_dt, title)

    event_str = event_dt.strftime("%m월 %d일 %H:%M (한국 표준시)")

    if not scheduled_times:
        return jsonify(send_text_response(
            f"⚠️ '{title}' 알림을 설정할 수 없습니다.\n"
            f"이벤트 시간({event_str})이 10분 이내로 너무 가깝습니다.\n"
            "최소 11분 이후의 일정을 입력해 주세요."
        ))

    times_str = " / ".join(f"{t} KST" for t in scheduled_times)
    return jsonify(send_text_response(
        f"✅ 알림이 설정되었습니다!\n"
        f"📅 제목: {title}\n"
        f"🕐 이벤트: {event_str}\n"
        f"🔔 자동 알림 발송 시간: {times_str}\n"
        f"(이벤트 10분 전, 5분 전에 자동으로 알림이 발송됩니다)"
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
        "scheduled_jobs_count": len(jobs),
        "scheduled_jobs": jobs,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
