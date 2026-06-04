import os
import re
import hmac
import hashlib
import logging
import requests
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SOLAPI_API_KEY = os.environ.get("SOLAPI_API_KEY")
SOLAPI_API_SECRET = os.environ.get("SOLAPI_API_SECRET")
SOLAPI_SENDER = os.environ.get("SOLAPI_SENDER")  # Your registered sender number

KST = pytz.timezone("Asia/Seoul")

scheduler = BackgroundScheduler(
    jobstores={"default": MemoryJobStore()},
    timezone=KST,
)
if not scheduler.running:
    scheduler.start()


def solapi_auth_header():
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    salt = os.urandom(16).hex()
    message = now + salt
    sig = hmac.new(
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


def send_sms(phone_number, text):
    url = "https://api.solapi.com/messages/v4/send"
    payload = {
        "message": {
            "to": phone_number,
            "from": SOLAPI_SENDER,
            "text": text,
            "type": "SMS",
        }
    }
    resp = requests.post(url, headers=solapi_auth_header(), json=payload, timeout=10)
    if resp.status_code not in (200, 201):
        logger.error("SMS failed: %s %s", resp.status_code, resp.text)
    else:
        logger.info("SMS sent to %s", phone_number)


def schedule_reminders(phone_number, event_dt, event_name):
    now_kst = datetime.now(tz=KST)
    scheduled = []
    for minutes_before in (60, 10):
        fire_at = event_dt - timedelta(minutes=minutes_before)
        if fire_at <= now_kst:
            continue
        job_id = f"{phone_number}_{event_name}_{minutes_before}min"
        msg = (
            f"[YA! 알림] '{event_name}' {minutes_before}분 후 시작됩니다! "
            f"({event_dt.strftime('%m/%d %H:%M')})"
        )
        scheduler.add_job(
            send_sms,
            "date",
            run_date=fire_at,
            args=[phone_number, msg],
            id=job_id,
            replace_existing=True,
        )
        scheduled.append(f"{minutes_before}분 전")
        logger.info("Scheduled %s for %s", job_id, fire_at)
    return scheduled


def text_response(text):
    return jsonify({
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}]
        }
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    user_input = data.get("userRequest", {}).get("utterance", "").strip()

    # Strip optional "알림" prefix (e.g. "알림 01012345678 06/15 이벤트명")
    if user_input.startswith("알림"):
        user_input = user_input[2:].strip()

    # Accepted formats:
    #   01012345678 06/15 이벤트명
    #   01012345678 06/15/09:00 이벤트명   (slash separator)
    #   01012345678 06/15 09:00 이벤트명   (space separator) ← FIX: now supported
    pattern = r"^(01[0-9]{8,9})\s+(\d{2}/\d{2})(?:[/ ](\d{2}:\d{2}))?\s+(.+)$"
    match = re.match(pattern, user_input)

    if not match:
        return text_response(
            "입력 형식이 올바르지 않아요.\n\n"
            "📌 올바른 형식:\n"
            "알림 전화번호 MM/DD 이벤트명\n"
            "알림 전화번호 MM/DD HH:MM 이벤트명\n\n"
            "✏️ 예시:\n"
            "알림 01012345678 06/15 기말고사\n"
            "알림 01012345678 06/15 09:00 기말고사"
        )

    phone = match.group(1)
    date_str = match.group(2)       # MM/DD
    time_str = match.group(3)       # HH:MM or None
    event_name = match.group(4).strip()

    # Default to 09:00 if no time provided
    time_str = time_str or "09:00"
    now_kst = datetime.now(tz=KST)
    year = now_kst.year

    try:
        event_dt = KST.localize(
            datetime.strptime(f"{year}/{date_str}/{time_str}", "%Y/%m/%d/%H:%M")
        )
        # If the date already passed this year, schedule for next year
        if event_dt < now_kst:
            event_dt = event_dt.replace(year=year + 1)
    except ValueError:
        return text_response(
            "날짜 또는 시간 형식이 올바르지 않아요.\n"
            "예: 06/15 또는 06/15 09:00"
        )

    scheduled = schedule_reminders(phone, event_dt, event_name)

    if not scheduled:
        return text_response(
            f"'{event_name}' 일정이 이미 지났거나 너무 임박해서\n"
            "알림을 설정할 수 없어요."
        )

    times_str = ", ".join(scheduled)
    return text_response(
        f"✅ 알림이 등록되었습니다!\n\n"
        f"📌 이벤트: {event_name}\n"
        f"📅 날짜: {event_dt.strftime('%m월 %d일 %H:%M')}\n"
        f"📱 번호: {phone}\n"
        f"⏰ 알림: {times_str} 전에 SMS 발송"
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
