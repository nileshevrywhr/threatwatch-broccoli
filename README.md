# ThreatWatch

ThreatWatch is a threat monitoring MVP that tracks keywords (e.g., "ransomware", "breach") using Google Programmable Search and generates PDF reports. It uses a background worker system to handle time-consuming scans and report generation without blocking the main web API.

---

## Architecture: Background Workers 101

This project uses an asynchronous architecture to handle long-running tasks. Here is why and how it works:

*   **Web API (FastAPI):** Handles incoming HTTP requests (creating monitors, checking health). It **never** runs heavy tasks directly. Instead, it "enqueues" a task message to Redis and returns immediately.
*   **Redis (The Broker):** Acts as a message queue (buffer). It holds task messages until a worker is free to pick them up.
*   **Celery Worker:** A separate process that constantly watches Redis. When it sees a message (e.g., "scan monitor #123"), it picks it up, runs the search, generates the PDF, and sends the email. This can take seconds or minutes without affecting the API's speed.
*   **Celery Beat (The Scheduler):** Another separate process that acts like a "cron" clock. It doesn't run tasks itself. It simply wakes up every few minutes to check which monitors are due and tells the Worker (via Redis) to scan them.

---

## Supabase Setup

Before running the app, you need to create the database tables and storage bucket in your Supabase project.

### 1. Database Tables
Run the following SQL in the Supabase SQL Editor to create the required tables.

```sql
-- 1. Monitors Table
create table monitors (
  id uuid default gen_random_uuid() primary key,
  user_id uuid not null, -- References auth.users(id) in a real app
  query_text text not null,
  frequency text not null check (frequency in ('daily', 'weekly', 'monthly')),
  next_run_at timestamptz not null,
  active boolean default true,
  created_at timestamptz default now()
);

-- 2. Reports Table
create table reports (
  id uuid default gen_random_uuid() primary key,
  monitor_id uuid references monitors(id) not null,
  user_id uuid not null,
  pdf_url text,
  item_count int,
  created_at timestamptz default now()
);

-- 3. Searches Table (Audit Log)
create table searches (
  id uuid default gen_random_uuid() primary key,
  query_text text not null,
  status text,
  created_at timestamptz default now()
);
```

### 2. Storage Bucket
1.  Go to **Storage** in the Supabase Dashboard.
2.  Create a new bucket named **`reports`**.
3.  **Important:** Make the bucket **Public**.
4.  (Optional) Add a policy to allow read/write access if you are restricting RLS, but keeping it Public is sufficient for the MVP to generate public download links.

---

## Local Development Setup

Follow these steps to run the entire system on your machine.

### 1. Prerequisites
*   Python 3.10+
*   **Redis:** You can run it locally (`redis-server`) or use a cloud instance (Upstash).
*   **Supabase Project:** For Database and Storage.
*   **Google Custom Search Engine (CSE):** API Key and Search Engine ID (CX).
*   **Gmail Account:** For sending emails (using an App Password).

### 2. Environment Variables
Create a `.env` file in the root directory.

**Recommended for Local Dev & MVP:** Use Gmail SMTP.
*   **Important:** You must use a [Google App Password](https://myaccount.google.com/apppasswords), NOT your regular Gmail password.

```ini
# Database (Supabase)
SUPABASE_URL=your_supabase_project_url
SUPABASE_SERVICE_ROLE_KEY=your_supabase_service_role_key

# Search (Google)
GOOGLE_CSE_API_KEY=your_google_api_key
GOOGLE_CSE_CX=your_search_engine_id

# Redis (Broker)
# Option A: Local Redis
REDIS_URL=redis://localhost:6379/0
# Option B: Upstash (Cloud) - Recommended to match prod
# REDIS_URL=rediss://default:password@your-upstash-instance:port

# Celery (Must match REDIS_URL)
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0

# Email (Gmail SMTP)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USERNAME=your_email@gmail.com
SMTP_PASSWORD=your_app_password
EMAIL_FROM=ThreatWatch <your_email@gmail.com>

# Feature Flags
DISABLE_SCHEDULER=false
ENABLE_EMAIL_DELIVERY=false
# Set specific retention policy if needed (default 30 days)
RETENTION_DAYS=30
```

### 3. Run the Services
You need to run **three separate terminal windows** to start the full system.

**Terminal 1: Web API**
```bash
uvicorn main:app --reload
```
*   Runs on `http://127.0.0.1:8000`.

**Terminal 2: Celery Worker**
```bash
celery -A celery_app worker --loglevel=info
```
*   Listens for tasks and executes scans.

**Terminal 3: Celery Beat (Scheduler)**
```bash
celery -A celery_app beat --loglevel=info
```
*   Schedules recurring scans for due monitors.

### 4. How to Test Locally
1.  **Create a Monitor:** Send a POST request to `http://127.0.0.1:8000/api/monitors` with a JSON body (use Postman or curl).
2.  **Trigger a Scan:** The API will automatically trigger an immediate scan.
3.  **Check Logs:** Look at **Terminal 2 (Worker)**. You should see "Received task: scan_monitor_task" followed by logs about searching and PDF generation.
4.  **Verify Output:** Check your Supabase `reports` table and Storage bucket for the new PDF. Check your email inbox.

---

## Deployment Guide (Railway)

We deploy this project as **three separate services** within a single Railway project. All three services pull from the same GitHub repository but run different start commands.

### 1. Railway Project Structure

*   **Service 1: `web-api`** (The FastAPI Backend)
*   **Service 2: `celery-worker`** (The Task Runner)
*   **Service 3: `celery-beat`** (The Scheduler)

### 2. Step-by-Step Setup

1.  **Create Project:** Go to Railway and create a new project.
2.  **Add Database:** Add a Redis service (or use Upstash externally).
3.  **Add Service (Web API):**
    *   Select "GitHub Repo".
    *   Connect this repository.
    *   Name the service `web-api`.
4.  **Add Service (Worker):**
    *   Click "New" -> "GitHub Repo" -> Select the **SAME** repository again.
    *   Name this service `celery-worker`.
5.  **Add Service (Beat):**
    *   Click "New" -> "GitHub Repo" -> Select the **SAME** repository a third time.
    *   Name this service `celery-beat`.

### 3. Configure Start Commands

Go to the "Settings" tab for each service and set the **Custom Start Command**:

| Service Name | Start Command |
| :--- | :--- |
| **web-api** | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| **celery-worker** | `celery -A celery_app worker --loglevel=info` |
| **celery-beat** | `celery -A celery_app beat --loglevel=info` |

### 4. Configure Environment Variables

Add these variables to the "Shared Variables" (or individually for each service).

*   **SUPABASE_URL** & **SUPABASE_SERVICE_ROLE_KEY** (from Supabase)
*   **GOOGLE_CSE_API_KEY** & **GOOGLE_CSE_CX** (from Google Cloud)
*   **CELERY_BROKER_URL** & **CELERY_RESULT_BACKEND** (Set these to your Upstash/Railway Redis URL, e.g., `redis://...`)
*   **SMTP Settings** (Same as local dev, use Gmail App Password)

### 5. Free-Tier Considerations
*   **Resource Limits:** The free tier has limited execution minutes.
*   **Sleep Behavior:** Railway services may "sleep" if inactive. The `web-api` will wake up on request, but the `celery-worker` and `celery-beat` might need to be kept alive or upgraded to a paid plan if you need 24/7 reliability.
*   **Logs:** Check the "Deploy Logs" tab in Railway to debug issues.

### 6. Verification
1.  **Health Check:** Visit `https://your-project.up.railway.app/health/celery`. It should return `{"redis": "ok", "celery": "ok"}`.
2.  **Worker Check:** In Railway logs for `celery-worker`, ensure it says `[config] .> app: threatwatch` and is ready.
3.  **Beat Check:** In Railway logs for `celery-beat`, ensure it is sending tasks every 5 minutes (`scan_due_monitors`).

---

## Troubleshooting

### Common Failure Modes

*   **Redis Connection Error:**
    *   *Symptom:* Logs show "Connection refused" or "Error connecting to Redis".
    *   *Fix:* Check `CELERY_BROKER_URL`. Ensure your Redis instance is running and reachable.

*   **Worker Not Picking Jobs:**
    *   *Symptom:* API returns success, but no report/email appears.
    *   *Fix:* Check `celery-worker` logs. If the worker is crashing or silent, tasks are just piling up in Redis. Restart the worker.

*   **Beat Running but Not Enqueuing:**
    *   *Symptom:* Scheduled scans never happen.
    *   *Fix:* Ensure `celery-beat` is running. Check if `DISABLE_SCHEDULER` is accidentally set to `true`.

*   **API Running but Async Jobs Fail:**
    *   *Symptom:* API works, but `scan_monitor_task` fails immediately.
    *   *Fix:* This usually means the Worker is missing env vars (like Google Keys or Supabase URL) that the API has. Ensure **all** services have the same environment variables.
