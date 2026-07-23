# 🤖 Browser Automation Bot — GrantFox + Drips Wave

Automatically logs into GrantFox and Drips Wave via GitHub OAuth, clicks the
Apply button on open issues, monitors for assignment, cancels on timeout, and
sends every update to Telegram.

---

## ⚡ Quick Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Configure your `.env`
```bash
cp .env.example .env
```
Fill in your credentials — see `.env.example` for details.

### 3. Login once (saves your session)
```bash
# Both platforms
python bot.py --login

# Drips Wave only
python bot.py --login --platform drips

# GrantFox only
python bot.py --login --platform grantfox
```
A browser window opens. Log in, then press ENTER in the terminal.
**Do NOT close the browser before pressing ENTER.**

### 4. Run the bot
```bash
# Both platforms (default)
python bot.py

# Drips Wave only
python bot.py --platform drips

# GrantFox only
python bot.py --platform grantfox

# Run once then exit (good for testing or cron)
python bot.py --once
python bot.py --once --platform drips
```

---

## 🧩 All Commands

| Command | What it does |
|---|---|
| `python bot.py` | Run forever on both platforms |
| `python bot.py --platform drips` | Drips Wave only, run forever |
| `python bot.py --platform grantfox` | GrantFox only, run forever |
| `python bot.py --once` | Both platforms, single cycle then exit |
| `python bot.py --once --platform drips` | Drips only, single cycle |
| `python bot.py --login` | Login to both platforms |
| `python bot.py --login --platform drips` | Login to Drips only |
| `python bot.py --login --platform grantfox` | Login to GrantFox only |

---

## 🔄 What Happens Each Cycle

```
Every 30 minutes:

1. CHECK active applications
   ├─ Assigned to you? → 🎉 Telegram alert, mark done
   ├─ Timed out (>24h)? → clicks Withdraw, Telegram alert, hunts next
   └─ Still pending? → logs time remaining

2. HUNT new issues (if slots available)
   ├─ Scrapes open issues on selected platform(s)
   ├─ Skips already-tried issues
   └─ Clicks Apply → Telegram alert
```

---

## 📱 Telegram Notifications

| Event | Alert |
|---|---|
| Bot started | 🤖 startup message with config |
| Applied for issue | ✅ platform + title + URL + timeout countdown |
| Issue assigned | 🎉 platform + title + link |
| Application timed out | ⏰ cancelled + hunting next |
| Any error | ⚠️ error message |

---

## 🚀 Keep It Running 24/7

### Option A — Screen (quickest on any machine)
```bash
screen -S bot
python bot.py --platform drips
# Ctrl+A then D to detach
# screen -r bot to reattach
```

### Option B — Systemd (Linux VPS, recommended)
```ini
# /etc/systemd/system/issuebot.service
[Unit]
Description=Issue Bot
After=network.target

[Service]
WorkingDirectory=/path/to/browser-bot
ExecStart=/usr/bin/python3 bot.py --platform drips
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable issuebot
sudo systemctl start issuebot
sudo journalctl -fu issuebot   # view live logs
```

### Option C — GitHub Actions (free, no server needed)
```yaml
# .github/workflows/bot.yml
name: Issue Bot
on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt && python -m playwright install chromium
      - run: python bot.py --once --platform drips
        env:
          GITHUB_USERNAME: ${{ secrets.GITHUB_USERNAME }}
          GITHUB_PASSWORD: ${{ secrets.GITHUB_PASSWORD }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
```

---

## 🛠 Troubleshooting

### Apply button not found
The platform UI may have updated. Run the selector debugger:
```bash
python debug_selectors.py drips
python debug_selectors.py grantfox
```
Then update the selector strings in the `apply()` method of the relevant class in `bot.py`.

### Session expired / login fails
Delete the old session file and re-login:
```bash
del sessions\drips.json        # Windows
rm sessions/drips.json          # Mac/Linux
python bot.py --login --platform drips
```

### See what the browser is doing
Set `HEADLESS=false` in your `.env`, then run:
```bash
python bot.py --once --platform drips
```

---

## 📂 File Structure

```
browser-bot/
├── bot.py                  # main bot — all logic
├── debug_selectors.py      # finds correct CSS selectors when UI changes
├── .env                    # your credentials (never commit this!)
├── .env.example            # template
├── requirements.txt
├── sessions/               # saved browser sessions (auto-created)
│   ├── grantfox.json
│   └── drips.json
├── state.json              # persisted application state (auto-created)
└── bot.log                 # activity log (auto-created)
```

---

## ⚠️ Important Notes

- **Drips Wave Wave 6**: max 5 repo applications per wave per user
- **KYC**: Drips requires KYC to withdraw earnings — complete in Settings before wave ends
- **Session lifetime**: sessions last days to weeks — re-run `--login` if you get auth errors
- **`HEADLESS=false`**: always use this when debugging so you can see what's happening
