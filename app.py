import os
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

def send_text_response(text):
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}

# ---------------------------------------------------------------------------
# ★ INPUT FORMAT (must start with "세미나변경")
# ---------------------------------------------------------------------------

KEYWORD = "세미나변경"

FORMAT_HELP = (
    "🏛 세미나실 변경 방법:\n\n"
    "  세미나변경\n\n"
    "위 단어를 입력하시면 변경 양식을 안내해 드립니다."
)

CHANGE_FORM = (
    "🏛 세미나실 알림 변경 양식입니다!\n\n"
    "아래 내용을 작성해서 보내주세요 😊\n\n"
    "1. 이름\n"
    "2. 기존 예약한 세미나실 날짜\n"
    "3. 변경 원하는 세미나실 날짜\n\n"
    "보내주신 내용은 24시간 이내로 확인 후,\n"
    "확정 메시지를 보내드립니다 🔔"
)

@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_json(silent=True) or {}
    utterance = body.get("userRequest", {}).get("utterance", "").strip()
    logging.info("Utterance: %s", utterance)

    if not utterance.startswith(KEYWORD):
        return jsonify(send_text_response(FORMAT_HELP))

    return jsonify(send_text_response(CHANGE_FORM))

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
