# Decisions for Deployment & Local Dev Guide (Authoritative)

## 1. File Location for the Guide

**Decision: Append to `README.md`.**

Why:

* Single source of truth
* New contributors always read README first
* No risk of outdated DEPLOYMENT.md drifting away
* Faster for MVP

Structure recommendation inside README:

```
README.md
├── Overview
├── Architecture
├── Deployment Guide
│   ├── Services
│   ├── Environment Variables
│   ├── Production Deployment (Railway / Render)
│   └── Health Checks
├── Local Development Setup
└── Troubleshooting
```

---

## 2. SMTP Configuration – Pick a Service First

### Decision: **Recommend Gmail SMTP for MVP + local dev**

Why Gmail SMTP:

* Everyone already has it
* Zero signup friction
* Works immediately
* Perfect for low-volume transactional email
* Easy to replace later

### What the guide should say

**Primary recommendation (MVP / local dev):**

* Gmail SMTP (personal or Google Workspace)

**Mention as future alternatives (briefly):**

* SendGrid
* Amazon SES
* Resend

But **do not document them in detail** yet.

### Gmail SMTP values (authoritative)

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USERNAME=your_email@gmail.com
SMTP_PASSWORD=app_password   # NOT your normal password
EMAIL_FROM=ThreatWatch <your_email@gmail.com>
```

Important note Jules must include:

* “Use a Google App Password, not your main Gmail password”
* “Email volume is low; this is safe for MVP”

This is pragmatic and launch-friendly.

---

## 3. Service Naming

**Decision: Your proposed names are correct and should be used.**

Approved service names:

* `web-api`
* `celery-worker`
* `celery-beat`

Why this matters:

* Clear separation of concerns
* Easy mental model
* Maps directly to process responsibilities
* Matches Railway/Render conventions

Jules should consistently use these names everywhere:

* README
* Diagrams
* Railway / Render screenshots
* Commands

---

## 4. Start Commands

Your proposed commands are **correct**.

Approved commands (no changes):

### Web API

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

### Celery Worker

```bash
celery -A celery_app worker --loglevel=info
```

### Celery Beat (Scheduler)

```bash
celery -A celery_app beat --loglevel=info
```

Important instruction for the guide:

* **These are three separate services**
* They must **not** be run in the same process
* They can be restarted independently

---

## 5. Add a Proper “Local Development Setup” Section (Important)

Yes — absolutely add this. This is non-negotiable.

### What the Local Setup section MUST include

#### 1. Prerequisites

* Python 3.10+
* Redis (local or Upstash)
* Supabase project
* Google CSE credentials
* Gmail App Password

#### 2. Environment Variables

Example `.env` template:

```
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=

GOOGLE_CSE_API_KEY=
GOOGLE_CSE_CX=

REDIS_URL=redis://localhost:6379/0

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USERNAME=
SMTP_PASSWORD=
EMAIL_FROM=

DISABLE_SCHEDULER=false
ENABLE_EMAIL_DELIVERY=false
```

#### 3. Local Redis Options

Explain both:

* Option A: `redis-server` locally
* Option B: Upstash Redis (recommended to mirror prod)

#### 4. How to run locally (step-by-step)

```bash
# Terminal 1
uvicorn main:app --reload

# Terminal 2
celery -A celery_app worker --loglevel=info

# Terminal 3
celery -A celery_app beat --loglevel=info
```

#### 5. How to test locally

* Create monitor via API
* Trigger scan
* Check logs
* Download PDF
* Check email (if enabled)

This section will save you **days** later.

---

## 6. What Jules Should Explain (Background Workers 101)

Since I don’t know enough about background workers yet, Jules should include a **plain-English explanation**:

### Required explanation in README

* Why background workers are needed for scans
* Why Celery is separate from the API
* Why Redis is needed
* What Celery Beat does vs Worker
* Why scheduled jobs must not run in web servers

Keep it short, but clear.

This helps:

* Me
* Future contributors
* Anyone reviewing the repo

---

## Copy-Paste Instruction for Jules

You can send this block directly:

> Append the deployment guide to README.md. Recommend Gmail SMTP for MVP/local dev (with App Password instructions), briefly mention SendGrid/SES as future options. Use service names web-api, celery-worker, celery-beat consistently. Document the exact start commands provided. Add a Local Development Setup section covering env vars, Redis options, how to run all three processes locally, and basic background worker explanations.

