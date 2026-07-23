# 🤖 IssueBot — GrantFox + Drips Wave Automation

Automatically applies for open issues on GrantFox and Drips Wave,
monitors assignment, cancels on timeout, and sends Telegram notifications.
Includes a live web dashboard your friends can view from anywhere.

---

## 📁 File Structure

```
IssueBot/
├── bot.py              ← browser automation (runs on your PC)
├── dashboard.py        ← web dashboard server (deploy to Render)
├── run.py              ← starts both bot + dashboard together
├── debug_selectors.py  ← debugging tool for CSS selectors
├── requirements.txt    ← all Python dependencies
├── render.yaml         ← Render deploy config
├── .env                ← your credentials (never commit!)
├── .env.example        ← template for .env
├── .gitignore          ← protects secrets from git
│
├── templates/          ← dashboard HTML pages
│   ├── index.html      ← main dashboard UI
│   └── login.html      ← password login page
│
├── sessions/           ← browser sessions (auto-created)
│   ├── grantfox.json
│   └── drips.json
│
├── state.json          ← bot state (auto-created)
└── bot.log             ← activity log (auto-created)
```

---

## ⚡ Quick Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Configure your .env
```bash
copy .env.example .env
```
Fill in your GitHub credentials, Telegram tokens, etc.

### 3. Login once (saves your browser session)
```bash
# Both platforms
python bot.py --login

# Drips Wave only
python bot.py --login --platform drips

# GrantFox only
python bot.py --login --platform grantfox
```
Browser opens → log in → press ENTER → session saved.
**Do NOT close the browser before pressing ENTER.**

### 4. Run everything
```bash
# Start bot + dashboard together (recommended)
python run.py

# Or run separately:
python bot.py              # bot only
python dashboard.py        # dashboard only (http://localhost:5000)
```

---

## 🧩 All Bot Commands

| Command | What it does |
|---|---|
| `python run.py` | Start bot + dashboard (both platforms) |
| `python run.py --platform drips` | Drips Wave only |
| `python run.py --platform grantfox` | GrantFox only |
| `python run.py --once` | Run one cycle then exit |
| `python bot.py --login` | Login to both platforms |
| `python bot.py --login --platform drips` | Login to Drips only |

---

## 🌐 Deploy Dashboard to Render (Free)

So your friends can see the dashboard from anywhere:

### Step 1 — Push this repo to GitHub
```bash
git add .
git commit -m "IssueBot"
git push
```

### Step 2 — Deploy on Render
1. Go to [render.com](https://render.com) → Sign up free
2. New → Web Service → Connect your GitHub repo
3. Render auto-detects `render.yaml` → click Deploy

### Step 3 — Set environment variables on Render
Go to your Render service → Environment → Add:
```
DASHBOARD_PASSWORD    = (password to share with friends)
DASHBOARD_SECRET_KEY  = (any random string)
SYNC_SECRET           = (must match SYNC_SECRET in your .env)
```

### Step 4 — Add Render URL to your .env
```env
DASHBOARD_URL=https://your-app-name.onrender.com
SYNC_SECRET=same-secret-as-render
```

Now every time the bot runs, it pushes stats to Render.
Friends open your URL, enter the password, see everything live.

---

## 📱 Telegram Notifications

| Event | Message |
|---|---|
| Bot started | Startup message with config |
| Applied for issue | Platform + title + URL + countdown |
| Issue assigned to you | Title + link |
| Application timed out | Cancelled + hunting next |
| Error | Error message |

---

## 🛠 Troubleshooting

### Apply button not found / 0 issues found
Run the selector debugger:
```bash
python debug_selectors.py grantfox
python debug_selectors.py drips
```

### Session expired
```bash
# Delete old session and re-login
del sessions\grantfox.json
python bot.py --login --platform grantfox
```

### See what the browser is doing
Set `HEADLESS=false` in `.env` then run normally.

### GrantFox shows "NOT REGISTERED"
Go to https://contribute.grantfox.xyz/issues and click REGISTER manually.
The bot will detect this and alert you on Telegram.

---

## ⚠️ Notes

- **Drips Wave Wave 6**: max 5 applications per wave per user
- **Always review** before marking assigned issues as done
- **Sessions** last days to weeks — re-run `--login` if auth fails
- **Render free tier** sleeps after 15 min inactivity — first visit takes ~30s to wake up
