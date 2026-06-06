# V5 Deployment via GitHub Actions — Step by Step

This is your deployment path now that Oracle Cloud is region-locked. Total setup time: ~30 minutes. Zero ongoing maintenance.

## What you'll have at the end

- Private GitHub repo holding your V5 code + data
- 4 scheduled jobs running automatically:
  - **Daily refresh** (weekdays 21:30 IST) — downloads bhavcopy, refreshes fundamentals, generates signals
  - **Weekly signal** (Mondays 09:10 IST) — Telegram broadcast with new entries + stops + confidence
  - **Daily exit check** (weekday evenings 16:30 IST) — Telegram alert if stops fire
  - **Weekly summary** (Saturdays 09:00 IST) — Telegram PnL summary
- Free forever within GitHub's free tier (we use ~165 of 2,000 free minutes/month)
- No SSH keys, no VM to manage, nothing breaks on its own

---

## Part 1 — GitHub account (5 min)

1. Go to **github.com** → Sign up (or sign in)
2. Use your email + a strong password
3. Verify email
4. **Optional but recommended:** turn on 2FA (Settings → Password and authentication → Two-factor authentication)

---

## Part 2 — Create the repository (3 min)

1. Top-right → **+** → **New repository**
2. **Repository name:** `v5-stockpicker`
3. **Visibility:** **Private** ⚠️ (must be private — your repo will contain trade signals)
4. **Initialize this repository with:** leave all unchecked
5. Click **Create repository**

You'll land on an empty repo page. Keep this tab open.

---

## Part 3 — Push your local code (10 min)

You need Git installed on your PC. If you don't have it:
- Download from **git-scm.com/download/win** → install with defaults → restart PowerShell

Open PowerShell and run:

```powershell
cd "C:\Users\ektan\OneDrive\Documents\Claude\Projects\Automated Stock Picker Model"

git init
git branch -M main
git add v5_modules .github requirements.txt .gitignore Stock_Picker_Audit_and_Refinement_Blueprint.docx V5_Action_Checklist.md GitHub_Actions_Setup_Guide.md Oracle_Cloud_Setup_Guide.md
git commit -m "V5 initial commit"
```

Now connect to GitHub. Replace `<your-username>` with your actual GitHub username:

```powershell
git remote add origin https://github.com/<your-username>/v5-stockpicker.git
git push -u origin main
```

GitHub will prompt for credentials. Use your GitHub username and a **Personal Access Token** (not your password):

1. Go to GitHub → top-right profile → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)** → **Generate new token (classic)**
2. **Note:** "v5-push"
3. **Expiration:** 90 days
4. **Scopes:** check `repo` (full repo access)
5. Generate → copy the token (starts with `ghp_`) — paste this as the password when git asks

Once pushed, refresh the GitHub repo page — you should see all your files.

---

## Part 4 — Add Telegram secrets (5 min)

GitHub stores secrets encrypted; workflows can read them but you can't view them after saving.

1. On your repo page → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**
3. Add two secrets one at a time:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token (from BotFather, looks like `1234567:AAH...`) |
| `TELEGRAM_CHAT_ID` | Your channel ID (looks like `-1001234567890` for channels, or your user ID for DMs) |

If you don't know your chat ID:
- Add your bot to your channel as admin
- Send a test message in the channel
- Visit: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
- Find `"chat":{"id":-1001234567890,...}` — that's your chat ID

---

## Part 5 — Upload your symbols list (5 min)

The system needs to know which NSE stocks to track. Two options:

### Option A — Use Nifty 500 (recommended start, ~500 stocks)

In PowerShell:

```powershell
# Download Nifty 500 stock list
Invoke-WebRequest -Uri "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv" -OutFile "nifty500.csv"

# Extract just the symbol column
Import-Csv nifty500.csv | Select-Object -ExpandProperty Symbol | Out-File -FilePath symbols.txt -Encoding utf8
```

### Option B — Use your full V4 universe (~2,876 stocks)

If you have `master_history.csv` from your V4 zip:

```powershell
# Extract unique symbols from master_history.csv (slow but thorough)
python -c "import pandas as pd; pd.read_csv('master_history.csv')['SYMBOL'].drop_duplicates().to_csv('symbols.txt', index=False, header=False)"
```

Either way, you'll end up with `symbols.txt` in your project folder. Add it to git:

```powershell
git add symbols.txt
git commit -m "Add NSE symbols list"
git push
```

---

## Part 6 — Bootstrap fundamentals (one-time, 30-60 min)

Run this **once** to populate the Screener.in cache for your universe. Subsequent daily refreshes are fast.

1. On GitHub repo page → **Actions** tab
2. Left sidebar → click **V5 Fundamentals Bootstrap (one-time / quarterly)**
3. Right side → **Run workflow** button → **Run workflow**
   - `force_refresh`: false
   - `symbols_limit`: 500 (or higher if you want to wait longer)
4. Click **Run workflow**

Watch progress: refresh the page, click on the running workflow, then click the `bootstrap` job. You'll see live log output.

After ~30 minutes for 500 stocks, the job commits the populated `screener.sqlite` back to your repo. Done.

(If you have 2,000+ symbols, split into multiple runs of 500 each — GitHub's runner has a 6-hour cap.)

---

## Part 7 — Verify everything works (5 min)

### 7.1 Trigger a manual daily refresh

1. Repo → Actions → **V5 Daily Data Refresh** → **Run workflow** → **Run workflow**
2. Wait ~5 minutes
3. Check the log — should show: bhavcopy downloaded → signals generated → data committed

### 7.2 Send a test Telegram message

1. Repo → Actions → **V5 Weekly Signal Broadcast** → **Run workflow** → **Run workflow**
2. Within 30 seconds, the broadcast should land on your Telegram channel

If the test message arrives, **everything is wired up correctly.** From this point, the system runs automatically on the schedule:

| Job | When (UTC) | When (IST) |
|---|---|---|
| Daily refresh | 16:00 Mon-Fri | 21:30 Mon-Fri |
| Daily exit check | 11:00 Mon-Fri | 16:30 Mon-Fri |
| Weekly signal | 03:40 Mon | 09:10 Mon |
| Weekly summary | 03:30 Sat | 09:00 Sat |

---

## Part 8 — Mobile monitoring

Everything you need lives in Telegram. But if you want to peek at the workflow runs:

- **GitHub mobile app** (free, Play Store / App Store) → log in → your repo → Actions tab → see runs, logs, success/failure
- Notifications: GitHub will email you if a workflow fails (Settings → Notifications → Actions)

---

## Costs

GitHub free tier:
- **Private repo unlimited storage** (we use ~50 MB)
- **2,000 Actions minutes/month** for private repos (we use ~165)
- **2 GB GitHub Packages** (we don't use)

You will not exceed the free tier with this setup.

---

## Routine maintenance

| What | How often | How |
|---|---|---|
| Check that Saturday's summary message arrived | Weekly | Glance at Telegram |
| Refresh full fundamentals | Quarterly | Manual trigger of `Fundamentals Bootstrap` workflow with `force_refresh=true` |
| Update V5 modules | When I send improvements | Pull from your PC, commit, push |
| Review failed runs | Whenever you get a "FAILED" Telegram alert | GitHub Actions tab → click the red ❌ run → read logs |

---

## If something breaks

| Symptom | Most likely cause | Fix |
|---|---|---|
| No Monday signal | Workflow failed silently | Actions tab → check `weekly_signal` last run |
| Failure alert in Telegram | Bhavcopy URL changed / NSE blocked GitHub IPs | Check `bhavcopy_downloader.py` logs in the failed run |
| Permission denied on git push step | `GITHUB_TOKEN` permissions reset | Repo Settings → Actions → General → "Workflow permissions" → "Read and write permissions" |
| Workflow not running on schedule | Cron uses UTC and GitHub sometimes delays by up to 1 hour | Wait 60 mins; if still nothing, manually trigger via "Run workflow" |
| "Module not found" errors | A new dependency wasn't added to requirements.txt | Add the package to requirements.txt, commit, push |

### Common first-run fix

GitHub disables workflows on schedules by default for newly-imported repos until you trigger them manually once. After your first manual run of each workflow, scheduling kicks in automatically.

---

## Eventually moving to Oracle Hyderabad

When you're ready (no rush), the steps:
1. Sign up at oracle.com/cloud/free with a **different email + phone number**, picking Hyderabad as home region
2. Provision an ARM Ampere VM following the Oracle_Cloud_Setup_Guide.md (Part 1-3)
3. Pull your repo on the Oracle VM: `git clone https://<token>@github.com/<your-username>/v5-stockpicker.git`
4. Set up cron exactly as the Oracle guide describes
5. Disable GitHub Actions workflows: Repo Settings → Actions → General → Disable Actions

Until then, GitHub Actions is your production environment.

---

*Last updated: 06 Jun 2026*
