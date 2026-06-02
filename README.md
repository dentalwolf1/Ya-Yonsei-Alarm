# Kakao Business Channel Reminder Bot

Sends users a reminder 10 minutes and 5 minutes before an event they register via chat.

## Setup

### 1. Kakao side (one-time)

1. Create a **Kakao Business Channel** at https://business.kakao.com
2. Go to **카카오 i 오픈빌더** (https://i.kakao.com) and create a chatbot connected to that channel
3. In Open Builder, add a **fallback block** (폴백 블록) or a free-text scenario that calls your webhook:
   - Skill URL: `https://your-server.com/webhook`
   - Method: POST
4. Enable **"친구에게 메시지 보내기"** in channel settings to allow push messages
5. Copy your **Channel Access Token** from: Business Channel > Settings > Development > API

### 2. Server setup

```bash
cd kakao-reminder-bot
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and fill in KAKAO_CHANNEL_ACCESS_TOKEN
```

### 3. Run

```bash
python app.py
```

Expose the server publicly (ngrok works for testing):

```bash
ngrok http 5000
# Use the https://xxxx.ngrok.io/webhook URL in Open Builder
```

### 4. Test

Send a message in KakaoTalk like:

```
2026-06-10 14:30 팀 미팅
```

The bot replies with a confirmation, then sends push alerts at T-10min and T-5min.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/webhook` | Kakao i Open Builder skill endpoint |
| GET | `/health` | Lists scheduled jobs |

## Input format examples

- `2026-06-10 14:30 팀 미팅`
- `6월 10일 오후 2시 30분 발표`
- `2026-06-15 09:00 주간 회의`

## Notes

- Jobs are stored in memory — restarting the server clears all pending reminders.
  For production, swap `MemoryJobStore` in `app.py` for `SQLAlchemyJobStore` with a database.
- The Kakao push message API requires users to have added your channel as a friend.
