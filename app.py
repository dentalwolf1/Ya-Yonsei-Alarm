import os
import re
import requests
import hashlib
import hmac
import time
import json
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
scheduler = BackgroundScheduler(misfire_grace_time=300)
scheduler.start()

# ── Solapi credentials ──────────────────────────────────────────────────────
SOLAPI_API_KEY    = os.environ.get("SOLAPI_API_KEY")
SOLAPI_API_SECRET = os.environ.get("SOLAPI_API_SECRET")
SOLAPI_SENDER     = os.environ.get("SOLAPI_SENDER")

# ── Self keep-alive ─────────────────────────────────────────────────────────
SELF_URL = os.environ.get("SELF_URL", "https://ya-yonsei-alarm-1.onrender.com")

def keep_alive():
    """Ping own /ping endpoint every 10 minutes to prevent Render cold-start."""
    if not SELF_URL:
        return
    try:
        resp = requests.get(f"{SELF_URL}/ping", timeout=10)
        print(f"[keep-alive] {datetime.now().strftime('%H:%M:%S')} → {resp.status_code}")
    except Exception as e:
        print(f"[keep-alive] ping failed: {e}")

scheduler.add_job(keep_alive, "interval", minutes=10, id="keep_alive")

@app.route("/ping")
def ping():
    return "pong", 200

# ── Solapi SMS sender ───────────────────────────────────────────────────────
def send_sms(to: str, text: str):
    # Guard: check env vars are loaded
    if not SOLAPI_API_KEY or not SOLAPI_API_SECRET or not SOLAPI_SENDER:
        print(f"[SMS] ❌ MISSING ENV VARS — KEY={SOLAPI_API_KEY} SECRET={'set' if SOLAPI_API_SECRET else 'MISSING'} SENDER={SOLAPI_SENDER}")
        return

    try:
        date_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        salt      = os.urandom(32).hex()  # 64-char random hex
        signature = hmac.new(
            SOLAPI_API_SECRET.encode(),
            f"{date_str}{salt}".encode(),
            hashlib.sha256
        ).hexdigest()

        headers = {
            "Authorization": f"HMAC-SHA256 apiKey={SOLAPI_API_KEY}, date={date_str}, salt={salt}, signature={signature}",
            "Content-Type": "application/json"
        }
        payload = {
            "message": {
                "to":   to,
                "from": SOLAPI_SENDER,
                "text": text,
                "type": "SMS"
            }
        }
        r = requests.post(
            "https://api.solapi.com/messages/v4/send",
            headers=headers,
            json=payload,
            timeout=10
        )
        print(f"[SMS] ✅ to={to} status={r.status_code} body={r.text}")
    except Exception as e:
        print(f"[SMS] ❌ EXCEPTION sending to {to}: {e}")

# ── Alarm scheduler ─────────────────────────────────────────────────────────
def schedule_alarm(phone: str, event_dt: datetime, event_name: str):
    now = datetime.now()

    candidates = [
        (event_dt - timedelta(minutes=60), "60분"),
        (event_dt - timedelta(minutes=10), "10분"),
    ]

    scheduled = []
    for remind_dt, label in candidates:
        if remind_dt > now:
            msg = f"[야! 연세 알람] '{event_name}' {label} 후에 시작됩니다!"
            job_id = f"{phone}_{event_name}_{int(remind_dt.timestamp())}"
            scheduler.add_job(
                send_sms,
                "date",
                run_date=remind_dt,
                args=[phone, msg],
                id=job_id,
                replace_existing=True
            )
            scheduled.append(label)
            print(f"[scheduler] job set: {job_id} at {remind_dt}")
        else:
            print(f"[scheduler] skipped {label} reminder — already past ({remind_dt} <= {now})")

    return scheduled  # e.g. ["60분", "10분"] or ["10분"] or []

# ── Webhook ─────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data        = request.get_json(force=True)
    user_input  = data.get("userRequest", {}).get("utterance", "").strip()
    print(f"[DEBUG] raw utterance: {repr(user_input)}")

    # Expected format: 알림 전화번호 MM/DD[/HH:MM] 이벤트명
    # Strip leading '알림' prefix
    if user_input.startswith("알림"):
        user_input = user_input[len("알림"):].strip()

    # Parse: phone  date[/time]  event_name
    pattern = r"^(\d{10,11})\s+(\d{1,2}/\d{1,2})(?:/(\d{1,2}:\d{2}))?\s+(.+)$"
    m = re.match(pattern, user_input)

    if not m:
        return jsonify({
            "version": "2.0",
            "template": {
                "outputs": [{
                    "simpleText": {
                        "text": (
                            "입력 형식이 올바르지 않아요.\n\n"
                            "형식: 알림 전화번호 MM/DD 이벤트명\n"
                            "예시: 알림 01012345678 06/15 팀 미팅\n"
                            "시간 포함: 알림 01012345678 06/15/14:30 팀 미팅"
                        )
                    }
                }]
            }
        })

    phone      = m.group(1)
    date_str   = m.group(2)   # MM/DD
    time_str   = m.group(3)   # HH:MM or None
    event_name = m.group(4).strip()

    month, day = map(int, date_str.split("/"))
    hour, minute = (9, 0) if not time_str else map(int, time_str.split(":"))

    now  = datetime.now()
    year = now.year
    try:
        event_dt = datetime(year, month, day, hour, minute)
    except ValueError:
        return jsonify({
            "version": "2.0",
            "template": {"outputs": [{"simpleText": {"text": "날짜가 올바르지 않아요. 다시 입력해주세요."}}]}
        })

    # Roll to next year if date already passed
    if event_dt <= now:
        event_dt = event_dt.replace(year=year + 1)

    scheduled = schedule_alarm(phone, event_dt, event_name)

    if not scheduled:
        reply = (
            f"☑️ 알림이 등록되었습니다!\n\n"
            f"📌 이벤트: {event_name}\n"
            f"🗓 날짜: {event_dt.strftime('%m월 %d일 %H:%M')}\n"
            f"📱 번호: {phone}\n"
            f"⚠️ 알림: 이벤트가 10분 이내라 문자 발송이 없습니다"
        )
    else:
        reminder_label = ", ".join(scheduled) + " 전에 SMS 발송"
        reply = (
            f"☑️ 알림이 등록되었습니다!\n\n"
            f"📌 이벤트: {event_name}\n"
            f"🗓 날짜: {event_dt.strftime('%m월 %d일 %H:%M')}\n"
            f"📱 번호: {phone}\n"
            f"⏰ 알림: {reminder_label}"
        )

    return jsonify({
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": reply}}]}
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
