# V5 Deployment on Oracle Cloud — Step by Step

End-to-end guide for someone who has used Windows but never managed a Linux server. Total time: ~2 hours one-time setup, then ~10 minutes a week.

## What you'll have at the end

- An Oracle Cloud Ampere VM (ARM, 24 GB RAM, free forever) running 24/7
- A daily cron job that downloads NSE bhavcopies, refreshes fundamentals from Screener.in, computes V5 scores
- A weekly Monday-morning Telegram broadcast with signals + confidence + stops
- A daily evening Telegram broadcast (only if exit signals fire)
- A Saturday morning summary broadcast
- Everything visible from your phone via the Telegram app

---

## Part 1 — Provision the Oracle Cloud VM (30 min)

### 1.1 Create the VM

1. Sign in at **cloud.oracle.com** (Mumbai or Hyderabad region).
2. Top-left menu → **Compute** → **Instances** → **Create Instance**.
3. Name: `v5-stockpicker`
4. **Image and shape** → Change shape → "Ampere" → pick **VM.Standard.A1.Flex**.
   - Set OCPUs: **4**, Memory: **24 GB**. (This is the free tier limit.)
5. **Networking** → leave defaults, but check "Assign a public IPv4 address" (it should be on by default).
6. **SSH keys** → **Generate a key pair for me** → click **Save Private Key** (filename like `ssh-key-v5.key`). Keep this file safe.
7. **Boot volume** → leave default Ubuntu 22.04.
8. Click **Create**. Wait 2-3 minutes for provisioning.

### 1.2 Get the public IP

Once running, the Instance page shows **Public IPv4 Address** — copy it. Looks like `129.151.x.x`.

### 1.3 Connect from your PC

**Windows:** Open PowerShell, navigate to where you saved the SSH key, run:

```
ssh -i ssh-key-v5.key ubuntu@<your_public_ip>
```

First time, type `yes` when prompted about the fingerprint. You should see `ubuntu@v5-stockpicker:~$`. You are now on the cloud server.

**If connection fails:** Oracle Cloud blocks port 22 inbound by default. In the Oracle Console:
- Networking → Virtual Cloud Networks → your VCN → Security Lists → Default Security List
- Add Ingress Rule: Source `0.0.0.0/0`, Protocol TCP, Destination port `22`
- Try SSH again.

---

## Part 2 — Install dependencies (10 min)

Once SSH'd into the VM, run these commands one block at a time:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv git unzip sqlite3
python3 -m venv ~/v5env
source ~/v5env/bin/activate
pip install --upgrade pip
pip install pandas numpy pyarrow requests beautifulsoup4 lxml python-dateutil
```

Verify:
```bash
python3 -c "import pandas, requests, bs4; print('OK')"
```

---

## Part 3 — Upload your V5 code (10 min)

You have two options:

### Option A — Push to a private GitHub repo first (recommended)

On your PC:
```
cd "C:\Users\ektan\OneDrive\Documents\Claude\Projects\Automated Stock Picker Model"
git init
git add v5_modules Oracle_Cloud_Setup_Guide.md V5_Action_Checklist.md
git commit -m "V5 modules"
```
Then create a private GitHub repo and push.

On the Oracle VM:
```bash
git clone https://github.com/<your-username>/<repo>.git ~/v5
```

### Option B — Direct SCP from your PC (simpler if no GitHub yet)

On your PC PowerShell:
```
scp -i ssh-key-v5.key -r "C:\Users\ektan\OneDrive\Documents\Claude\Projects\Automated Stock Picker Model\v5_modules" ubuntu@<public_ip>:~/v5
```

Verify on Oracle:
```bash
ls ~/v5
# Should see all the .py files
```

---

## Part 4 — Configure Telegram secrets (5 min)

On the Oracle VM:
```bash
mkdir -p ~/v5/secrets
nano ~/v5/secrets/telegram_creds.txt
```

Paste:
```
BOT_TOKEN=<your_bot_token>
CHAT_ID=<your_channel_id>
```

Save (Ctrl+O, Enter, Ctrl+X), then lock the file:
```bash
chmod 600 ~/v5/secrets/telegram_creds.txt
```

### Send a test message

```bash
source ~/v5env/bin/activate
cd ~/v5
python3 telegram_broadcaster.py --type test --creds ~/v5/secrets/telegram_creds.txt
```

Check your Telegram channel — you should see "V5 system test" within a few seconds.

---

## Part 5 — Bootstrap the fundamentals cache (60 min, one-time)

This pre-fetches Screener.in data for your ~2,000 NSE symbols. Takes about an hour due to rate limiting (1 req per 2 sec). Run once, refresh daily after.

### 5.1 Get the symbol list

On the Oracle VM:
```bash
cd ~/v5
# If you uploaded your master_history.csv from the StockPicker zip, extract symbols:
# python3 -c "import pandas as pd; df=pd.read_csv('~/v5/data/master_history.csv'); df['SYMBOL'].drop_duplicates().to_csv('symbols.txt', index=False, header=False)"
# Or use the Nifty 500 list:
wget -O nifty500.csv https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv
python3 -c "import pandas as pd; df=pd.read_csv('nifty500.csv'); df['Symbol'].to_csv('symbols.txt', index=False, header=False)"
```

### 5.2 Run the bootstrap

```bash
nohup python3 screener_scraper.py --bootstrap symbols.txt --cache-dir ~/v5/data/fundamentals > ~/v5/data/bootstrap.log 2>&1 &
```

Monitor progress: `tail -f ~/v5/data/bootstrap.log`. Will print "[100/500] ok=98 fail=2 elapsed=3.4m ETA=13.5m" lines.

After ~30-60 minutes, check completion:
```bash
sqlite3 ~/v5/data/fundamentals/screener.sqlite "SELECT COUNT(*) FROM sc WHERE json_extract(payload, '$._fetched_ok') = 1"
```

### 5.3 Spot check

Inspect one stock you know:
```bash
python3 screener_scraper.py --one BIOCON --cache-dir ~/v5/data/fundamentals
```

You should see JSON with `roce_pct: 6.25`, `promoter_drop_qoq_pp: ~15.7`, etc.

---

## Part 6 — Set up the cron jobs (10 min)

On the Oracle VM:
```bash
crontab -e
```

(Pick `nano` if asked for editor.) Add these lines at the bottom, then save:

```cron
# Refresh fundamentals + price data nightly 21:30 IST = 16:00 UTC
0 16 * * * /home/ubuntu/v5env/bin/python /home/ubuntu/v5/daily_data_refresh.py >> /home/ubuntu/v5/logs/daily.log 2>&1

# Monday pre-market signal 09:10 IST = 03:40 UTC
40 3 * * 1 /home/ubuntu/v5env/bin/python /home/ubuntu/v5/telegram_broadcaster.py --type weekly_signal --creds /home/ubuntu/v5/secrets/telegram_creds.txt >> /home/ubuntu/v5/logs/signal.log 2>&1

# Trading-day evening exit check 16:30 IST = 11:00 UTC (Mon-Fri)
0 11 * * 1-5 /home/ubuntu/v5env/bin/python /home/ubuntu/v5/telegram_broadcaster.py --type daily_exit --creds /home/ubuntu/v5/secrets/telegram_creds.txt >> /home/ubuntu/v5/logs/exit.log 2>&1

# Saturday weekly summary 09:00 IST = 03:30 UTC
30 3 * * 6 /home/ubuntu/v5env/bin/python /home/ubuntu/v5/telegram_broadcaster.py --type weekly_summary --creds /home/ubuntu/v5/secrets/telegram_creds.txt >> /home/ubuntu/v5/logs/summary.log 2>&1
```

Create the logs directory:
```bash
mkdir -p ~/v5/logs
```

---

## Part 7 — Smoke test the full pipeline (5 min)

Force-run the weekly signal once manually:
```bash
source ~/v5env/bin/activate
cd ~/v5
python3 telegram_broadcaster.py --type weekly_signal \
    --portfolio ./data/live_signals/daily_live_portfolio.csv \
    --creds ./secrets/telegram_creds.txt --dry-run
```

`--dry-run` prints the message without sending. Review it. If it looks right, remove `--dry-run` and run again — the message will land on Telegram.

---

## Part 8 — Mobile monitoring (ongoing)

You don't need to SSH into the server to check on it. Two options:

### Option A — Termius app on your phone
- Free app from Play Store / App Store
- Add your Oracle VM with the same SSH private key
- Tap to connect, run `tail -f ~/v5/logs/daily.log` if you want to peek

### Option B — Telegram is enough
- Every Monday you get the signal
- Every trading day evening you get exits (if any)
- Every Saturday you get a summary
- If you stop getting messages, SSH in once a month and check `~/v5/logs/`

---

## Costs

Oracle Always Free tier: **₹0/month forever**, as long as:
- You stay within the ARM Ampere quota (1 VM, 4 OCPU, 24 GB RAM, 200 GB block storage)
- You log in to the Oracle Console every 30 days (otherwise the account can be flagged inactive)

Cron + Python + ~2 GB data fits comfortably within free tier.

---

## Routine maintenance

| What | How often | How |
|---|---|---|
| Log into Oracle Console | Monthly | Prevents account being marked inactive |
| Check `~/v5/logs/` size | Monthly | If > 1 GB, `find ~/v5/logs -type f -mtime +30 -delete` |
| Re-bootstrap fundamentals | Quarterly | `python3 screener_scraper.py --bootstrap symbols.txt --force` |
| Update V5 modules | When I send a new version | `git pull` (Option A) or scp (Option B) |

---

## If something breaks

| Symptom | Most likely cause | Fix |
|---|---|---|
| No Telegram messages on Monday | Cron clock wrong | `timedatectl status` — should show UTC; cron times in guide are UTC |
| "TELEGRAM_BOT_TOKEN missing" | Creds file path wrong in cron | Edit `crontab -e`, fix `--creds` path |
| Scraper fails on all stocks | Screener.in changed HTML or blocked IP | Check `~/v5/data/fundamentals/scrape.log`; reduce request rate or rotate IP |
| SSH connection refused | Oracle security list reset | Re-add port 22 ingress rule |
| Cron not running | VM rebooted | `sudo systemctl status cron` and `crontab -l` to verify |

---

## What this guide does NOT cover (yet)

- Automated order placement to your broker (we chose manual execution for now)
- Live PnL tracking (you'll add this once you start real trading)
- Backup of fundamentals SQLite (consider `rclone sync ~/v5/data <your-cloud>` weekly)

We'll add these only after paper trading proves the system has edge.

---

*Last updated: 27 May 2026*
