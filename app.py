import os
import re
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

# ---------------------------------------------------------------------------
# Timezone — ALWAYS Korean Standard Time (UTC+9), regardless of server region.
# The Render server may be in Oregon (PST/PDT) but every datetime the user
# enters and every alarm that fires is anchored to KST.
# ---------------------------------------------------------------------------
KST = pytz.timezone("Asia/Seoul")
TIMEZONE = KST

# Use a single shared scheduler. gunicorn is started with --workers 1
# (see Procfile) to prevent each worker from creating its own scheduler
# and firing duplicate reminders.
scheduler = BackgroundScheduler(
    jobstores={"default": MemoryJobStore()},
    timezone=KST,          # scheduler internal clock runs on KST
)

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
    """Schedule T-10min and T-5min reminder jobs for a user.
    event_dt must be a KST-aware datetime.
    """
    now_kst = datetime.now(tz=KST)

    for minutes_before in (10, 5):
        fire_at = event_dt - timedelta(minutes=minutes_before)
        if fire_at <= now_kst:
            logger.info("Skipping %d-min reminder — already past (KST)", minutes_before)
            continue

        job_id = f"{user_id}_{event_label}_{minutes_before}min"
        message = f"⏰ 알림: '{event_label}' 이벤트가 {minutes_before}분 후 시작됩니다!"

        scheduler.add_job(
            send_push_message,
            trigger="date",
            run_date=fire_at,   # APScheduler receives a KST-aware datetime
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
# Korean datetime normaliser
# ---------------------------------------------------------------------------

def normalize_korean_datetime(text: str) -> str:
    """
    Translate common Korean datetime expressions into English so that
    dateparser can understand them reliably.

    Supported patterns:
      오전/오후 N시 (N분)   →  N AM / N:M PM
      N월 N일               →  N/D
      내일 / 모레 / 오늘    →  tomorrow / day after tomorrow / today
      N시간 후              →  in N hours
      N분 후                →  in N minutes
      N시 N분 (bare)        →  N:M
    """
    t = text

    # Relative day words
    t = re.sub(r'내일', 'tomorrow', t)
    t = re.sub(r'모레', 'day after tomorrow', t)
    t = re.sub(r'오늘', 'today', t)

    # Relative durations
    t = re.sub(r'(\d+)\s*시간\s*후', r'in \1 hours', t)
    t = re.sub(r'(\d+)\s*분\s*후',   r'in \1 minutes', t)

    # 오후/오전 + hour + minute
    t = re.sub(r'오후\s*(\d+)\s*시\s*(\d+)\s*분', r'\1:\2 PM', t)
    t = re.sub(r'오후\s*(\d+)\s*시',              r'\1:00 PM', t)
    t = re.sub(r'오전\s*(\d+)\s*시\s*(\d+)\s*분', r'\1:\2 AM', t)
    t = re.sub(r'오전\s*(\d+)\s*시',              r'\1:00 AM', t)

    # Month / day
    t = re.sub(r'(\d+)\s*월\s*(\d+)\s*일', r'\1/\2', t)

    # Bare N시 N분 (no 오전/오후)
    t = re.sub(r'(\d+)\s*시\s*(\d+)\s*분', r'\1:\2', t)
    t = re.sub(r'(\d+)\s*시',              r'\1:00', t)

    # Strip leftover Korean grammatical particles
    t = re.sub(r'[에의은는이가을를로으로에서까지]', ' ', t)

    return t.strip()


# ---------------------------------------------------------------------------
# Datetime parser — always returns a KST-aware datetime
# ---------------------------------------------------------------------------

def parse_event_datetime(text: str) -> datetime | None:
    """
    Parse Korean or English free-form text into a KST-aware datetime.

    The user enters time in Korean Standard Time (UTC+9).
    The server clock (Oregon) is irrelevant — all comparisons and scheduling
    are done in KST.

    Rolls forward automatically if the parsed time is already in the past:
      - Same date, past time  → push to tomorrow (same time, KST)
      - Past date             → push to next year
    """
    try:
        now_kst = datetime.now(tz=KST)

        normalized = normalize_korean_datetime(text)
        logger.info("Datetime text normalised: '%s' → '%s'", text, normalized)

        # dateparser settings force interpretation as KST input
        settings = {
            "PREFER_DATES_FROM": "future",
            "PREFER_DAY_OF_MONTH": "first",
            "TIMEZONE": "Asia/Seoul",          # treat bare times as KST
            "TO_TIMEZONE": "Asia/Seoul",        # return value in KST
            "RETURN_AS_TIMEZONE_AWARE": True,
        }

        dt = dateparser.parse(normalized, fuzzy=True, dayfirst=False, settings=settings)

        # Fallback: try the original (untranslated) text
        if dt is None:
            dt = dateparser.parse(text, fuzzy=True, settings=settings)

        if dt is None:
            return None

        # Ensure timezone is KST (convert if necessary)
        if dt.tzinfo is None:
            dt = KST.localize(dt)
        else:
            dt = dt.astimezone(KST)

        # Roll forward if still in the past
        if dt <= now_kst:
            if dt.date() == now_kst.date():
                # Only the time was given and it already passed today → tomorrow
                dt = dt + timedelta(days=1)
            if dt <= now_kst:
                # Full date was in the past → next year
                dt = dt.replace(year=dt.year + 1)

        logger.info("Parsed KST datetime: %s", dt.strftime("%Y-%m-%d %H:%M %Z"))
        return dt

    except (ValueError, OverflowError) as e:
        logger.warning("parse_event_datetime error: %s", e)
        return None


def extract_label(text: str, dt: datetime) -> str:
    """Use the raw user text as the event label (fallback to formatted KST time)."""
    label = text.strip()
    if not label:
        label = dt.strftime("%Y-%m-%d %H:%M KST")
    return label


# ---------------------------------------------------------------------------
# Webhook endpoint (called by Kakao i Open Builder)
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_json(silent=True) or {}
    logger.info("Incoming webhook: %s", body)

    user_request = body.get("userRequest", {})
    user_id      = user_request.get("user", {}).get("id", "")
    utterance    = user_request.get("utterance", "").strip()

    if not utterance:
        return jsonify(send_text_response(
            "이벤트 날짜와 시간을 입력해 주세요. (한국 표준시 기준)\n"
            "예: 6월 10일 오후 2시 30분 팀 미팅\n"
            "예: 내일 오전 9시 스탠드업"
        ))

    event_dt = parse_event_datetime(utterance)

    if event_dt is None:
        return jsonify(send_text_response(
            "날짜와 시간을 인식하지 못했습니다. 다시 시도해 주세요.\n"
            "예: '6월 10일 오후 2시 30분 팀 미팅'\n"
            "예: '내일 오전 9시 회의'"
        ))

    now_kst = datetime.now(tz=KST)
    if event_dt <= now_kst:
        return jsonify(send_text_response(
            "입력하신 시간이 이미 지났습니다. (한국 표준시 기준)\n미래 일정을 입력해 주세요."
        ))

    label = extract_label(utterance, event_dt)
    schedule_reminders(user_id, event_dt, label)

    # Display time back to user in KST
    formatted = event_dt.strftime("%Y년 %m월 %d일 %H:%M (한국 표준시)")
    return jsonify(send_text_response(
        f"✅ 알림이 설정되었습니다!\n"
        f"📅 이벤트: {label}\n"
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
