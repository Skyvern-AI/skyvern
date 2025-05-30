# Complete Setup Commands for Cron Functionality

This document walks you through **every shell command** required to pull the new branch, install dependencies, run database migrations, and start both backend and frontend so that the workflow-cron feature is live.

---

## 1  Get the Code

```bash
# 1.1 Clone or fetch latest sources
git clone git@github.com:Skyvern-AI/skyvern.git   # if you don't have the repo yet
cd skyvern

# 1.2 Ensure all remotes are up-to-date
git remote update

# 1.3 Checkout the cron branch created earlier
git checkout kash/cron_one
```

---

## 2  Create/Activate Python Environment

```bash
# Using Python 3.11+ recommended
python -m venv .venv
source .venv/bin/activate
```

---

## 3  Install Backend Dependencies

```bash
# 3.1 Install Poetry if missing
pip install --upgrade poetry

# 3.2 Install project deps (incl. APScheduler, croniter, pytz)
poetry install --no-interaction --with dev
```

---

## 4  Configure Environment Variables

```bash
cp .env.example .env
# Edit .env → set DATABASE_URL, OPENAI_API_KEY, etc.
```

---

## 5  Run Database Migrations (CRITICAL!)

```bash
# 5.1 Upgrade schema to latest (adds cron columns & indexes)
alembic upgrade head
```

This applies `2025_05_29_1511_bf4a8c7d1e9a_add_cron_job_support.py` migration.

---

## 6  Start Backend Server

```bash
# 6.1 Launch Forge API with auto-reload
poetry run uvicorn skyvern.forge.api_app:app --host 0.0.0.0 --port 8000 --reload
```

During startup you should see:
```
INFO  Initializing workflow scheduler
INFO  Loaded <N> scheduled workflows
```

---

## 7  Start Frontend (optional)

```bash
cd skyvern-frontend
npm install          # first time only
npm run dev          # localhost:5173
# open http://localhost:5173 in browser
```

---

## 8  Verify Cron Scheduler

1. Log in to UI → **Workflows** → pick a workflow → **Schedule Workflow** card
2. Enable scheduling, e.g. `*/5 * * * *` in `UTC`
3. Observe `next scheduled run` timestamp
4. Watch backend logs when time passes → workflow run appears with `triggered_by_cron=true`

---

## 9  Run Tests

```bash
pytest tests/test_workflow_scheduler.py -v
```

You're now fully set up to use automated, cron-driven workflows in Skyvern 🎉

