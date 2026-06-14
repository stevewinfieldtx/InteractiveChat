"""
api.py — Guardz Research Agent + Live Demo
"""

import asyncio
import os
import re
from datetime import datetime

import asyncpg
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from company_research import (
    ResearchResult, domain_from_email, domain_from_url,
    research_company, research_industry,
)

app = FastAPI(title="Guardz Research Agent", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATABASE_URL = os.getenv("DATABASE_URL", "")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "agent_5101kty2ztmme25aspqycwp7mpsm")
ELEVENLABS_API_KEY  = os.getenv("VITE_ELEVENLABS_API_KEY", os.getenv("ELEVENLABS_API_KEY", ""))
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL    = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4-5")

# ─── Email handoff config ─────────────────────────────────────────────────────
# Who receives the handoff (the "sales team"). For now this is Steve.
SALES_TEAM_EMAIL = os.getenv("SALES_TEAM_EMAIL", "stevewinfieldtx@gmail.com")
SALES_REP_NAME   = os.getenv("SALES_REP_NAME", "Steve")
# Sender. Resend's onboarding@resend.dev works with no domain verification for testing.
EMAIL_FROM       = os.getenv("EMAIL_FROM", "Rain Networks <onboarding@resend.dev>")
# Provider A — Resend (preferred, easiest): set RESEND_API_KEY.
RESEND_API_KEY   = os.getenv("RESEND_API_KEY", "")
# Provider B — SMTP (e.g. Gmail app password): set SMTP_HOST/USER/PASS.
SMTP_HOST        = os.getenv("SMTP_HOST", "")
SMTP_PORT        = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER        = os.getenv("SMTP_USER", "")
SMTP_PASS        = os.getenv("SMTP_PASS", "")
# Send the lightweight "are you available?" ping on the first buying signal.
SEND_AVAILABILITY_PING = os.getenv("HANDOFF_SEND_AVAILABILITY", "true").lower() == "true"

# ─── Demo state (in-memory, resets on redeploy) ───────────────────────────────

_demo_jobs: list[dict] = []   # most-recent first, max 10
_demo_signals: list[dict] = []
_live_transcript: list[dict] = []
_active_conv_id: str = ""
_transcript_task: asyncio.Task | None = None
# Per-session handoff state so we ping availability once and email the brief once.
_handoff_state: dict[str, dict] = {}

def _upsert_demo_job(session_id: str, **fields):
    for job in _demo_jobs:
        if job["session_id"] == session_id:
            job.update(fields)
            return
    _demo_jobs.insert(0, {"session_id": session_id, **fields})
    if len(_demo_jobs) > 10:
        _demo_jobs.pop()


# ─── DB pool ──────────────────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None

def _fix_db_url(url: str) -> str:
    return url.replace("postgresql://", "postgres://", 1)

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(_fix_db_url(DATABASE_URL), min_size=2, max_size=10)
    return _pool

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS company_profiles (
    id SERIAL PRIMARY KEY, domain TEXT NOT NULL UNIQUE, company_name TEXT,
    description TEXT, industry TEXT, sub_industry TEXT, company_size TEXT,
    estimated_revenue TEXT, founded_year INT, hq_location TEXT, business_model TEXT,
    tech_stack_signals JSONB DEFAULT '[]', recent_news JSONB DEFAULT '[]',
    funding_stage TEXT, key_competitors JSONB DEFAULT '[]', linkedin_url TEXT,
    confidence TEXT DEFAULT 'low', research_notes TEXT,
    researched_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '30 days')
);
CREATE INDEX IF NOT EXISTS idx_company_profiles_domain ON company_profiles(domain);
CREATE INDEX IF NOT EXISTS idx_company_profiles_industry ON company_profiles(industry);

CREATE TABLE IF NOT EXISTS industry_profiles (
    id SERIAL PRIMARY KEY, industry TEXT NOT NULL, sub_industry TEXT NOT NULL DEFAULT '',
    UNIQUE (industry, sub_industry),
    top_pain_points JSONB DEFAULT '[]', buying_triggers JSONB DEFAULT '[]',
    common_objections JSONB DEFAULT '[]', key_metrics JSONB DEFAULT '[]',
    industry_trends JSONB DEFAULT '[]', regulatory_pressures JSONB DEFAULT '[]',
    typical_decision_makers JSONB DEFAULT '[]', average_sales_cycle TEXT,
    researched_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '14 days')
);
CREATE INDEX IF NOT EXISTS idx_industry_profiles_industry ON industry_profiles(industry);

CREATE TABLE IF NOT EXISTS research_jobs (
    id SERIAL PRIMARY KEY, session_id TEXT NOT NULL, domain TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ, duration_seconds FLOAT, error_message TEXT,
    company_profile_id INT REFERENCES company_profiles(id),
    industry_profile_id INT REFERENCES industry_profiles(id)
);
CREATE INDEX IF NOT EXISTS idx_research_jobs_session ON research_jobs(session_id);
CREATE INDEX IF NOT EXISTS idx_research_jobs_domain ON research_jobs(domain);
CREATE INDEX IF NOT EXISTS idx_research_jobs_status ON research_jobs(status);

CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY, session_id TEXT NOT NULL UNIQUE, solution_slug TEXT NOT NULL,
    contact_name TEXT, contact_email TEXT, contact_title TEXT, domain TEXT,
    research_job_id INT REFERENCES research_jobs(id), messages JSONB DEFAULT '[]',
    outcome TEXT, created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_conversations_email ON conversations(contact_email);
CREATE INDEX IF NOT EXISTS idx_conversations_outcome ON conversations(outcome);

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$ BEGIN NEW.updated_at = NOW(); RETURN NEW; END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_conversations_updated
    BEFORE UPDATE ON conversations FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TABLE IF NOT EXISTS partner_leads (
    id SERIAL PRIMARY KEY,
    session_id TEXT UNIQUE,
    partner_name TEXT,
    partner_company TEXT,
    partner_email TEXT,
    partner_phone TEXT,
    customer_vertical TEXT,
    last_signal TEXT,
    signal_count INT DEFAULT 0,
    handed_off BOOLEAN DEFAULT FALSE,
    brief TEXT,
    transcript JSONB DEFAULT '[]',
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    last_seen TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_partner_leads_email   ON partner_leads(partner_email);
CREATE INDEX IF NOT EXISTS idx_partner_leads_company ON partner_leads(partner_company);
CREATE INDEX IF NOT EXISTS idx_partner_leads_seen    ON partner_leads(last_seen DESC);
"""

@app.on_event("startup")
async def startup():
    if DATABASE_URL:
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(SCHEMA_SQL)
            print("[startup] DB schema ready")
        except Exception as e:
            print(f"[startup] DB not ready, will retry on first request: {e}")

@app.on_event("shutdown")
async def shutdown():
    if _pool:
        await _pool.close()


# ─── Request / Response models ────────────────────────────────────────────────

class ResearchRequest(BaseModel):
    domain: str | None = None
    email: str | None = None
    session_id: str = ""
    force_refresh: bool = False
    # Customer-targeted research (Rain Networks: research the partner's CUSTOMER,
    # not the partner). Provide an industry/vertical and/or a named client.
    industry: str | None = None
    customer_name: str | None = None
    customer_website: str | None = None

class ResearchStatusResponse(BaseModel):
    session_id: str
    domain: str
    status: str
    result: dict | None = None
    error: str | None = None


# ─── Cache helpers ────────────────────────────────────────────────────────────

async def _get_cached_company(pool, domain):
    row = await pool.fetchrow(
        "SELECT * FROM company_profiles WHERE domain=$1 AND expires_at>NOW()", domain)
    return dict(row) if row else None

async def _get_cached_industry(pool, industry, sub_industry):
    row = await pool.fetchrow(
        "SELECT * FROM industry_profiles WHERE industry=$1 AND sub_industry=$2 AND expires_at>NOW()",
        industry, sub_industry)
    return dict(row) if row else None

async def _save_company(pool, c) -> int:
    import json as _j
    row = await pool.fetchrow(
        """INSERT INTO company_profiles
           (domain,company_name,description,industry,sub_industry,company_size,
            estimated_revenue,founded_year,hq_location,business_model,
            tech_stack_signals,recent_news,funding_stage,key_competitors,
            linkedin_url,confidence,research_notes,researched_at,expires_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,NOW(),NOW()+INTERVAL '30 days')
           ON CONFLICT (domain) DO UPDATE SET
             company_name=EXCLUDED.company_name,description=EXCLUDED.description,
             industry=EXCLUDED.industry,sub_industry=EXCLUDED.sub_industry,
             company_size=EXCLUDED.company_size,estimated_revenue=EXCLUDED.estimated_revenue,
             founded_year=EXCLUDED.founded_year,hq_location=EXCLUDED.hq_location,
             business_model=EXCLUDED.business_model,tech_stack_signals=EXCLUDED.tech_stack_signals,
             recent_news=EXCLUDED.recent_news,funding_stage=EXCLUDED.funding_stage,
             key_competitors=EXCLUDED.key_competitors,linkedin_url=EXCLUDED.linkedin_url,
             confidence=EXCLUDED.confidence,research_notes=EXCLUDED.research_notes,
             researched_at=NOW(),expires_at=NOW()+INTERVAL '30 days'
           RETURNING id""",
        c.domain,c.company_name,c.description,c.industry,c.sub_industry,
        c.company_size,c.estimated_revenue,c.founded_year,c.hq_location,c.business_model,
        _j.dumps(c.tech_stack_signals),_j.dumps(c.recent_news),c.funding_stage,
        _j.dumps(c.key_competitors),c.linkedin_url,c.confidence,c.research_notes)
    return row["id"]

async def _save_industry(pool, i) -> int:
    import json as _j
    row = await pool.fetchrow(
        """INSERT INTO industry_profiles
           (industry,sub_industry,top_pain_points,buying_triggers,common_objections,
            key_metrics,industry_trends,regulatory_pressures,
            typical_decision_makers,average_sales_cycle,researched_at,expires_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW(),NOW()+INTERVAL '14 days')
           ON CONFLICT (industry,sub_industry) DO UPDATE SET
             top_pain_points=EXCLUDED.top_pain_points,buying_triggers=EXCLUDED.buying_triggers,
             common_objections=EXCLUDED.common_objections,key_metrics=EXCLUDED.key_metrics,
             industry_trends=EXCLUDED.industry_trends,regulatory_pressures=EXCLUDED.regulatory_pressures,
             typical_decision_makers=EXCLUDED.typical_decision_makers,
             average_sales_cycle=EXCLUDED.average_sales_cycle,
             researched_at=NOW(),expires_at=NOW()+INTERVAL '14 days'
           RETURNING id""",
        i.industry,i.sub_industry,
        _j.dumps(i.top_pain_points),_j.dumps(i.buying_triggers),
        _j.dumps(i.common_objections),_j.dumps(i.key_metrics_they_care_about),
        _j.dumps(i.industry_trends),_j.dumps(i.regulatory_pressures),
        _j.dumps(i.typical_decision_makers),i.average_sales_cycle)
    return row["id"]


# ─── Background research ──────────────────────────────────────────────────────

async def _run_research_job_no_db(domain: str, session_id: str):
    """Run research without database — demo/no-DB-URL mode."""
    try:
        result: ResearchResult = await research_company(domain)
        sc = result.sales_context
        _upsert_demo_job(
            session_id,
            status="done",
            domain=domain,
            company=result.company.model_dump(),
            industry=result.industry.model_dump(),
            sales_context=sc.model_dump() if sc else None,
            duration_seconds=result.duration_seconds,
        )
    except Exception as e:
        _upsert_demo_job(session_id, status="failed", error=str(e))
        print(f"[research] Failed for {domain}: {e}")


async def _run_customer_job(customer_domain: str, industry: str, customer_name: str, session_id: str):
    """Research the partner's CUSTOMER — a named client (by website) or a vertical/industry."""
    try:
        if customer_domain:
            result: ResearchResult = await research_company(customer_domain)
        else:
            result = await research_industry(industry or "Technology", "", customer_name or "")
        sc = result.sales_context
        _upsert_demo_job(
            session_id,
            status="done",
            domain=result.domain,
            company=result.company.model_dump(),
            industry=result.industry.model_dump(),
            sales_context=sc.model_dump() if sc else None,
            duration_seconds=result.duration_seconds,
        )
    except Exception as e:
        _upsert_demo_job(session_id, status="failed", error=str(e))
        print(f"[customer-research] Failed ({customer_name or industry or customer_domain}): {e}")


async def _run_research_job(job_id: int, domain: str, session_id: str):
    pool = await get_pool()
    await pool.execute(
        "UPDATE research_jobs SET status='running',started_at=NOW() WHERE id=$1", job_id)
    try:
        result: ResearchResult = await research_company(domain)
        company_id  = await _save_company(pool, result.company)
        industry_id = await _save_industry(pool, result.industry)
        await pool.execute(
            """UPDATE research_jobs SET status='done',completed_at=NOW(),
               duration_seconds=$1,company_profile_id=$2,industry_profile_id=$3
               WHERE id=$4""",
            result.duration_seconds, company_id, industry_id, job_id)

        # Update demo state with full results
        sc = result.sales_context
        _upsert_demo_job(
            session_id,
            status="done",
            domain=domain,
            company=result.company.model_dump(),
            industry=result.industry.model_dump(),
            sales_context=sc.model_dump() if sc else None,
            duration_seconds=result.duration_seconds,
        )
    except Exception as e:
        await pool.execute(
            "UPDATE research_jobs SET status='failed',error_message=$1,completed_at=NOW() WHERE id=$2",
            str(e), job_id)
        _upsert_demo_job(session_id, status="failed", error=str(e))
        raise


# ─── API Endpoints ────────────────────────────────────────────────────────────

@app.post("/research/company", response_model=ResearchStatusResponse)
async def start_research(req: ResearchRequest, background_tasks: BackgroundTasks):
    # ── Customer-targeted research (research the partner's CUSTOMER, not the partner) ──
    if req.industry or req.customer_name or req.customer_website:
        cust_domain = domain_from_url(req.customer_website) if req.customer_website else ""
        label = req.customer_name or cust_domain or (req.industry or "target vertical")
        _upsert_demo_job(
            req.session_id, domain=label, status="running",
            started_at=datetime.utcnow().isoformat(),
            company=None, industry=None, sales_context=None,
        )
        background_tasks.add_task(
            _run_customer_job, cust_domain, req.industry or "", req.customer_name or "", req.session_id
        )
        return ResearchStatusResponse(session_id=req.session_id, domain=label, status="pending")

    domain = req.domain or ""
    if not domain and req.email:
        domain = domain_from_email(req.email)
    if not domain:
        raise HTTPException(status_code=400, detail="Provide a 'domain' or business 'email'.")

    # Track in demo state immediately (before any DB ops)
    _upsert_demo_job(
        req.session_id,
        domain=domain,
        status="running",
        started_at=datetime.utcnow().isoformat(),
        company=None, industry=None, sales_context=None,
    )

    # No DB configured — run research directly without caching
    if not DATABASE_URL:
        background_tasks.add_task(_run_research_job_no_db, domain, req.session_id)
        return ResearchStatusResponse(session_id=req.session_id, domain=domain, status="pending")

    pool = await get_pool()

    if not req.force_refresh:
        cached = await _get_cached_company(pool, domain)
        if cached:
            ind = await _get_cached_industry(pool, cached["industry"] or "", cached["sub_industry"] or "")
            _upsert_demo_job(req.session_id, status="done",
                company=dict(cached), industry=dict(ind) if ind else {})
            return ResearchStatusResponse(
                session_id=req.session_id, domain=domain, status="done",
                result={"company": dict(cached), "industry": dict(ind) if ind else {}, "source": "cache"})

    job_id = await pool.fetchval(
        "INSERT INTO research_jobs (session_id,domain,status) VALUES ($1,$2,'pending') RETURNING id",
        req.session_id, domain)
    background_tasks.add_task(_run_research_job, job_id, domain, req.session_id)
    return ResearchStatusResponse(session_id=req.session_id, domain=domain, status="pending")


@app.get("/research/status/{session_id}", response_model=ResearchStatusResponse)
async def get_research_status(session_id: str):
    pool = await get_pool()
    job = await pool.fetchrow(
        """SELECT j.*,c.*,ip.* FROM research_jobs j
           LEFT JOIN company_profiles c ON c.id=j.company_profile_id
           LEFT JOIN industry_profiles ip ON ip.id=j.industry_profile_id
           WHERE j.session_id=$1 ORDER BY j.id DESC LIMIT 1""",
        session_id)
    if not job:
        raise HTTPException(status_code=404, detail="No research job found for this session")
    job = dict(job)
    if job["status"] == "done":
        return ResearchStatusResponse(session_id=session_id, domain=job["domain"], status="done",
            result={
                "company": {k: job[k] for k in ["domain","company_name","description","industry",
                    "sub_industry","company_size","estimated_revenue","founded_year","hq_location",
                    "business_model","tech_stack_signals","recent_news","funding_stage",
                    "key_competitors","linkedin_url","confidence","research_notes"] if k in job},
                "industry": {k: job[k] for k in ["top_pain_points","buying_triggers",
                    "common_objections","key_metrics","industry_trends","regulatory_pressures",
                    "typical_decision_makers","average_sales_cycle"] if k in job},
                "duration_seconds": job.get("duration_seconds")})
    if job["status"] == "failed":
        return ResearchStatusResponse(session_id=session_id, domain=job["domain"],
            status="failed", error=job.get("error_message"))
    return ResearchStatusResponse(session_id=session_id, domain=job["domain"], status=job["status"])


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ─── Slack / Human alert ──────────────────────────────────────────────────────

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SIGNAL_LABELS = {
    "pricing_question": "💰 Asked about pricing / contract terms",
    "demo_agreed":      "✅ Agreed to a demo",
    "how_to_start":     "🚀 Asked how to get started",
    "named_client":     "🎯 Named a specific client to start with",
    "strong_interest":  "🔥 Expressed strong buying intent",
    "handoff_requested":"🤝 Requested to speak with a human",
    "other":            "⚡ Close signal detected",
}

class NotifyHumanRequest(BaseModel):
    session_id: str = ""
    prospect_name: str = ""
    company_name: str = ""
    signal_type: str = "other"
    message: str = ""
    # Optional — populated once the agent collects them for the callback handoff.
    contact_email: str = ""
    contact_phone: str = ""

class NotifyHumanResponse(BaseModel):
    ok: bool
    available: bool = False
    rep_name: str = ""
    detail: str = ""


async def _generate_sales_brief(
    transcript: list, customer: dict, sales_context: dict,
    partner_name: str, partner_company: str
) -> str:
    """OpenRouter brief: help Steve (Rain Networks) close the PARTNER by speaking to their CUSTOMER's pains."""
    if not OPENROUTER_API_KEY or not transcript:
        return ""
    import httpx

    tx_lines = []
    for t in transcript:
        role = "AGENT" if t.get("role") == "agent" else "PARTNER"
        tx_lines.append(f"{role}: {t.get('message','').strip()}")
    tx_text = "\n".join(tx_lines)

    cu = customer or {}
    ctx = sales_context or {}
    market = cu.get("company_name") or ctx.get("industry") or ""
    customer_ctx = ""
    if market:
        customer_ctx = (
            f"Target market/vertical: {market}\n"
            f"Industry: {cu.get('industry','') or ctx.get('industry','')}\n"
            f"About: {(cu.get('description') or '')[:250]}"
        )
    pains = ", ".join((ctx.get("pain_points") or [])[:4])
    triggers = ", ".join((ctx.get("buying_triggers") or [])[:4])
    regs = ", ".join((ctx.get("regulatory_pressures") or [])[:3])
    if pains:
        customer_ctx += f"\nCustomer pain points: {pains}"
    if triggers:
        customer_ctx += f"\nBuying triggers: {triggers}"
    if regs:
        customer_ctx += f"\nCompliance pressures: {regs}"

    prompt = f"""You are a sales intelligence analyst at Rain Networks, a Guardz distributor. You're briefing {SALES_REP_NAME}, who is about to call back a PARTNER (an IT reseller) that is evaluating Guardz to sell to THEIR customers. The win is showing how Guardz solves the partner's CUSTOMERS' problems. Be specific to THIS conversation. No generic advice.

PARTNER ({SALES_REP_NAME} is calling them back): {partner_name or "Unknown"} at {partner_company or "Unknown"}

THEIR TARGET CUSTOMER MARKET (what Guardz must win for them):
{customer_ctx or "(not yet identified — flag pinning this down as the first move)"}

TRANSCRIPT:
{tx_text}

Respond in exactly this format (keep each section tight):

HIGHLIGHTS
• [most important thing revealed]
• [second most important]
• [third — only if genuinely distinct]

INTENT SCORE
[number 0-100]% — [one sentence: what signals drove this score]

DIRECTION
[1-2 sentences: where the partner is in the journey and what they're about to do]

BIGGEST CONCERN
[The single clearest objection or blocker — quote their words if possible]

QUESTIONS FOR {SALES_REP_NAME.upper()}
1. [Exact question to ask the partner] — WHY: [what this unlocks]
2. [Exact question to ask the partner] — WHY: [what this unlocks]
3. [Exact question to ask the partner] — WHY: [what this unlocks]"""

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "HTTP-Referer": "https://guardz-demo.up.railway.app",
                    "X-Title": "Guardz Sales Agent",
                },
                json={
                    "model": OPENROUTER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 700,
                    "temperature": 0.3,
                }
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            else:
                print(f"[brief] OpenRouter {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[brief] Error: {e}")
    return ""

# ─── Email helpers ────────────────────────────────────────────────────────────

_PHONE_RE = re.compile(r"(\+?\d[\d\-.\s()]{7,}\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _transcript_text(turns: list) -> str:
    return "\n".join(f"{t.get('role','')}: {t.get('message','')}" for t in (turns or []))


def _extract_phone(*sources) -> str:
    """Find the first plausible phone number (10-15 digits) across the sources."""
    for s in sources:
        if not s:
            continue
        for m in _PHONE_RE.finditer(str(s)):
            digits = re.sub(r"\D", "", m.group(1))
            if 10 <= len(digits) <= 15:
                return m.group(1).strip()
    return ""


def _extract_email(*sources) -> str:
    for s in sources:
        if not s:
            continue
        m = _EMAIL_RE.search(str(s))
        if m:
            return m.group(0).strip()
    return ""


def _html_to_text(html_str: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", html_str or "")
    return re.sub(r"\s+", " ", txt).strip()


def _smtp_send(to: str, subject: str, html_body: str, text_body: str):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = to
    msg.attach(MIMEText(text_body or _html_to_text(html_body), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    sender = EMAIL_FROM.split("<")[-1].strip(">").strip() if "<" in EMAIL_FROM else EMAIL_FROM
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.ehlo()
        try:
            server.starttls()
            server.ehlo()
        except Exception:
            pass
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(sender, [to], msg.as_string())


async def send_email(to: str, subject: str, html_body: str, text_body: str = "") -> tuple:
    """Send email via Resend (preferred) or SMTP. Returns (ok, detail)."""
    if not to:
        return False, "no recipient"
    if RESEND_API_KEY:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                             "Content-Type": "application/json"},
                    json={"from": EMAIL_FROM, "to": [to], "subject": subject,
                          "html": html_body, "text": text_body or _html_to_text(html_body)},
                )
            if resp.status_code in (200, 201):
                return True, "sent via resend"
            return False, f"resend {resp.status_code}: {resp.text[:160]}"
        except Exception as e:
            return False, f"resend error: {e}"
    if SMTP_HOST and SMTP_USER and SMTP_PASS:
        try:
            await asyncio.to_thread(_smtp_send, to, subject, html_body, text_body)
            return True, "sent via smtp"
        except Exception as e:
            return False, f"smtp error: {e}"
    print(f"[email] No provider configured — would send to {to}: {subject}")
    return False, "no email provider configured (set RESEND_API_KEY or SMTP_HOST/USER/PASS)"


def _brief_to_html(brief_text: str) -> str:
    import html as _h
    if not brief_text:
        return "<p style='color:#9ca3af;font-size:14px'>(AI brief unavailable — check OPENROUTER_API_KEY)</p>"
    rows = []
    for ln in brief_text.split("\n"):
        s = ln.strip()
        if not s:
            rows.append("<div style='height:6px'></div>")
            continue
        esc = _h.escape(s)
        if s.isupper() and len(s) <= 40:
            rows.append(f"<div style='font-size:12px;font-weight:700;letter-spacing:.06em;color:#7c3aed;margin:14px 0 4px'>{esc}</div>")
        else:
            rows.append(f"<div style='font-size:14px;color:#1f2937;line-height:1.6;margin:2px 0'>{esc}</div>")
    return "".join(rows)


def _transcript_to_html(turns: list) -> str:
    import html as _h
    if not turns:
        return "<p style='color:#9ca3af;font-size:14px'>(no transcript captured)</p>"
    rows = []
    for t in turns:
        is_agent = t.get("role") == "agent"
        who = "Agent" if is_agent else "Caller"
        color = "#7c3aed" if is_agent else "#0ea5e9"
        bg = "#f5f3ff" if is_agent else "#f0f9ff"
        msg = _h.escape((t.get("message") or "").strip())
        rows.append(
            f"<div style='margin:6px 0;padding:8px 12px;background:{bg};border-left:3px solid {color};border-radius:4px'>"
            f"<div style='font-size:11px;font-weight:700;color:{color};margin-bottom:2px'>{who}</div>"
            f"<div style='font-size:14px;color:#1f2937;line-height:1.5'>{msg}</div></div>"
        )
    return "".join(rows)


def _email_shell(inner: str) -> str:
    return (
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
        "max-width:640px;margin:0 auto;background:#ffffff;color:#1f2937;border:1px solid #eee;border-radius:10px;overflow:hidden\">"
        f"{inner}</div>"
    )


def _section(label: str, body_html: str) -> str:
    return (
        "<div style='margin:18px 0'>"
        f"<div style='font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#6b7280;margin-bottom:6px'>{label}</div>"
        f"<div>{body_html}</div></div>"
    )


def _customer_to_html(customer: dict) -> str:
    import html as _h
    if not customer or not (customer.get("name") or customer.get("industry")):
        return "<p style='color:#9ca3af;font-size:14px'>(not identified on the call — first thing to pin down)</p>"
    title = customer.get("name") or customer.get("industry")
    head = f"<div style='font-size:15px;color:#1f2937'><b>{_h.escape(str(title))}</b>"
    if customer.get("industry") and customer.get("industry") != title:
        head += f" <span style='color:#6b7280'>· {_h.escape(str(customer.get('industry')))}</span>"
    head += "</div>"
    def block(lbl, items, color):
        items = [i for i in (items or []) if i]
        if not items:
            return ""
        lis = "".join(f"<li style='margin:2px 0'>{_h.escape(str(x))}</li>" for x in items)
        return (f"<div style='margin-top:10px'><div style='font-size:11px;font-weight:700;"
                f"letter-spacing:.04em;color:{color}'>{lbl}</div>"
                f"<ul style='margin:4px 0 0 18px;padding:0;font-size:13px;color:#1f2937;line-height:1.5'>{lis}</ul></div>")
    return (head
            + block("PAIN POINTS", customer.get("pains"), "#b91c1c")
            + block("BUYING TRIGGERS", customer.get("triggers"), "#047857")
            + block("COMPLIANCE PRESSURES", customer.get("regulations"), "#b45309"))


def _build_availability_email(contact: dict, customer: dict, last_message: str, label: str) -> tuple:
    import html as _h
    name = _h.escape(contact.get("name") or "Unknown")
    company = _h.escape(contact.get("company") or "Unknown partner")
    target = customer.get("name") or customer.get("industry") or ""
    subject = f"⚡ Partner ready for a call — are you free? ({contact.get('name') or 'Unknown'} @ {contact.get('company') or 'Unknown'})"
    target_line = (f"<div style='font-size:14px;color:#1f2937;margin-top:4px'>Targeting: <b>{_h.escape(str(target))}</b></div>" if target else "")
    inner = (
        "<div style='background:#0d0d24;color:#fff;padding:20px 24px'>"
        "<div style='font-size:13px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#a78bfa'>Rain Networks · Guardz Handoff</div>"
        "<div style='font-size:20px;font-weight:800;margin-top:6px'>A partner looks ready to move</div></div>"
        "<div style='padding:8px 24px 24px'>"
        + _section("Partner", f"<div style='font-size:15px;color:#1f2937'><b>{name}</b> at <b>{company}</b></div>" + target_line)
        + _section("Signal", f"<div style='font-size:14px;color:#1f2937'>{_h.escape(label)}</div>")
        + _section("Last thing they said", f"<div style='font-size:14px;color:#1f2937;font-style:italic'>&ldquo;{_h.escape(last_message or '')}&rdquo;</div>")
        + f"<div style='margin-top:16px;padding:14px 16px;background:#f5f3ff;border-radius:8px;font-size:14px;color:#4c1d95'>"
          f"<b>{_h.escape(SALES_REP_NAME)}</b> — if you can take this, the AI is collecting the partner's callback number now. "
          "A full brief with their number and their customers' pain points follows in a moment.</div>"
        "</div>"
    )
    return subject, _email_shell(inner)


def _build_handoff_email(contact: dict, customer: dict, last_message: str,
                         brief_text: str, transcript: list, label: str) -> tuple:
    import html as _h
    name = contact.get("name") or "Unknown"
    company = contact.get("company") or "Unknown partner"
    email = contact.get("email") or "(not captured)"
    phone = contact.get("phone") or "(not captured)"
    subject = f"🔥 Call now: {name} @ {company} — callback {phone}"
    contact_html = (
        "<div style='font-size:14px;color:#1f2937;line-height:1.8'>"
        f"<b>Name:</b> {_h.escape(name)}<br>"
        f"<b>Partner / reseller:</b> {_h.escape(company)}<br>"
        f"<b>Email:</b> {_h.escape(email)}<br>"
        f"<b>Phone (call this):</b> <span style='font-size:17px;font-weight:700;color:#059669'>{_h.escape(phone)}</span>"
        "</div>"
    )
    trigger_html = (
        f"<div style='font-size:14px;color:#1f2937;font-style:italic'>&ldquo;{_h.escape(last_message or '')}&rdquo; "
        f"<span style='color:#9ca3af;font-style:normal'>({_h.escape(label)})</span></div>"
    )
    inner = (
        "<div style='background:#0d0d24;color:#fff;padding:20px 24px'>"
        "<div style='font-size:13px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#a78bfa'>Rain Networks · Hot Handoff</div>"
        f"<div style='font-size:20px;font-weight:800;margin-top:6px'>{_h.escape(SALES_REP_NAME)}, you're up — call this partner now</div></div>"
        "<div style='padding:8px 24px 24px'>"
        + _section("Contact (call the partner)", contact_html)
        + _section("Customer / target vertical — the real target", _customer_to_html(customer))
        + _section("What triggered the handoff", trigger_html)
        + _section("AI Sales Brief", _brief_to_html(brief_text))
        + _section(f"Transcript — last {len(transcript or [])} turns", _transcript_to_html(transcript))
        + "</div>"
    )
    return subject, _email_shell(inner)


# ─── Notify human (background-driven email + optional Slack) ───────────────────

HANDOFF_SIGNALS = ("handoff_requested", "strong_interest", "demo_agreed",
                   "named_client", "pricing_question", "how_to_start")


async def _save_lead(*, session_id, name, company, email, phone, vertical,
                     signal, handed_off, brief, transcript):
    """Progressively upsert a partner lead for follow-up + engagement BI. Best-effort."""
    if not DATABASE_URL:
        return
    try:
        import json as _j
        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO partner_leads
                (session_id, partner_name, partner_company, partner_email, partner_phone,
                 customer_vertical, last_signal, signal_count, handed_off, brief, transcript,
                 first_seen, last_seen)
            VALUES ($1,$2,$3,$4,$5,$6,$7,1,$8,$9,$10,NOW(),NOW())
            ON CONFLICT (session_id) DO UPDATE SET
                partner_name      = COALESCE(NULLIF(EXCLUDED.partner_name,''),      partner_leads.partner_name),
                partner_company   = COALESCE(NULLIF(EXCLUDED.partner_company,''),   partner_leads.partner_company),
                partner_email     = COALESCE(NULLIF(EXCLUDED.partner_email,''),     partner_leads.partner_email),
                partner_phone     = COALESCE(NULLIF(EXCLUDED.partner_phone,''),     partner_leads.partner_phone),
                customer_vertical = COALESCE(NULLIF(EXCLUDED.customer_vertical,''), partner_leads.customer_vertical),
                last_signal       = EXCLUDED.last_signal,
                signal_count      = partner_leads.signal_count + 1,
                handed_off        = partner_leads.handed_off OR EXCLUDED.handed_off,
                brief             = COALESCE(NULLIF(EXCLUDED.brief,''),             partner_leads.brief),
                transcript        = EXCLUDED.transcript,
                last_seen         = NOW()
            """,
            session_id or "unknown", name or "", company or "", email or "", phone or "",
            vertical or "", signal or "", handed_off, brief or "", _j.dumps(transcript or []),
        )
    except Exception as e:
        print(f"[leads] save failed: {e}")


@app.post("/notify/human", response_model=NotifyHumanResponse)
async def notify_human(req: NotifyHumanRequest):
    label = SIGNAL_LABELS.get(req.signal_type, SIGNAL_LABELS["other"])

    # Always track in demo state (drives the live dashboard)
    _demo_signals.insert(0, {
        "session_id": req.session_id,
        "prospect_name": req.prospect_name,
        "company_name": req.company_name,
        "signal_type": req.signal_type,
        "label": label,
        "message": req.message,
        "timestamp": datetime.utcnow().isoformat(),
    })
    if len(_demo_signals) > 20:
        _demo_signals.pop()

    # Heavy work (brief generation + email + Slack) runs in the background so the
    # voice agent's tool call returns instantly and the web conversation never stalls.
    asyncio.create_task(_process_notification(req, label))

    return NotifyHumanResponse(ok=True, available=True, rep_name=SALES_REP_NAME, detail="accepted")


async def _process_notification(req: NotifyHumanRequest, label: str):
    """Background worker: email handoff (availability ping + full brief) and optional Slack."""
    try:
        is_handoff = req.signal_type in HANDOFF_SIGNALS
        transcript = list(_live_transcript)
        job = _demo_jobs[0] if _demo_jobs else {}
        customer_co = job.get("company") or {}      # the researched CUSTOMER (vertical/client), NOT the partner
        sales_context = job.get("sales_context") or {}

        # Partner = the reseller on the call, who Steve calls back.
        recent_user_text = " ".join(
            t.get("message", "") for t in transcript[-6:] if t.get("role") != "agent"
        )
        contact = {
            "name": req.prospect_name or "",
            "company": req.company_name or "",
            "email": _extract_email(req.contact_email, req.message, _transcript_text(transcript)),
            "phone": _extract_phone(req.contact_phone, req.message, recent_user_text),
        }
        # Customer / target vertical the partner wants to win — the real research payload.
        customer = {
            "name": customer_co.get("company_name") or sales_context.get("industry") or "",
            "industry": customer_co.get("industry") or sales_context.get("industry") or "",
            "pains": (sales_context.get("pain_points") or [])[:5],
            "triggers": (sales_context.get("buying_triggers") or [])[:5],
            "regulations": (sales_context.get("regulatory_pressures") or [])[:4],
        }
        brief_text = ""

        # ── Email handoff ──────────────────────────────────────────────────────
        if is_handoff:
            st = _handoff_state.setdefault(
                req.session_id or "default", {"availability_sent": False, "brief_sent": False})

            if SEND_AVAILABILITY_PING and not st["availability_sent"]:
                subj, html = _build_availability_email(contact, customer, req.message, label)
                ok, detail = await send_email(SALES_TEAM_EMAIL, subj, html)
                st["availability_sent"] = True
                print(f"[handoff] availability email -> {SALES_TEAM_EMAIL}: ok={ok} ({detail})")

            # Send the full brief once we have a callback number (the callback step),
            # or immediately on an explicit handoff_requested signal.
            ready_for_brief = bool(contact["phone"]) or req.signal_type == "handoff_requested"
            if not st["brief_sent"] and ready_for_brief:
                brief_text = await _generate_sales_brief(
                    transcript=transcript, customer=customer_co, sales_context=sales_context,
                    partner_name=contact["name"], partner_company=contact["company"],
                )
                subj, html = _build_handoff_email(
                    contact=contact, customer=customer, last_message=req.message,
                    brief_text=brief_text, transcript=transcript[-8:], label=label,
                )
                ok, detail = await send_email(SALES_TEAM_EMAIL, subj, html)
                st["brief_sent"] = True
                print(f"[handoff] brief email -> {SALES_TEAM_EMAIL}: ok={ok} ({detail})")

        # ── Slack (optional, off unless SLACK_WEBHOOK_URL is set) ───────────────
        if SLACK_WEBHOOK_URL:
            await _post_slack(req, label, is_handoff, customer_co, sales_context, transcript)

        # ── Persist the lead for follow-up + engagement BI (every notify_human) ──
        await _save_lead(
            session_id=req.session_id,
            name=contact["name"], company=contact["company"],
            email=contact["email"], phone=contact["phone"],
            vertical=customer["industry"] or customer["name"],
            signal=req.signal_type,
            handed_off=is_handoff and bool(contact["phone"]),
            brief=brief_text,
            transcript=transcript,
        )
    except Exception as e:
        print(f"[notify] worker error: {e}")


async def _post_slack(req: NotifyHumanRequest, label: str, is_handoff: bool,
                      customer_co: dict, sales_context: dict, transcript: list):
    brief_text = ""
    if is_handoff:
        brief_text = await _generate_sales_brief(
            transcript=transcript, customer=customer_co, sales_context=sales_context,
            partner_name=req.prospect_name, partner_company=req.company_name)

    convo_url = (f"https://elevenlabs.io/app/conversational-ai/conversations/{req.session_id}"
                 if req.session_id else "")

    if is_handoff:
        blocks = [
            {"type":"header","text":{"type":"plain_text","text":f"🔥 HANDOFF — {SALES_REP_NAME}, you're up","emoji":True}},
            {"type":"section","fields":[
                {"type":"mrkdwn","text":f"*Prospect:*\n{req.prospect_name or '(not collected)'}"},
                {"type":"mrkdwn","text":f"*Company:*\n{req.company_name or '(unknown)'}"},
            ]},
            {"type":"section","text":{"type":"mrkdwn","text":f"*Signal:* {label}\n*Last thing they said:*\n> {req.message or ''}"}},
        ]
        if brief_text:
            blocks.append({"type":"divider"})
            blocks.append({"type":"section","text":{"type":"mrkdwn","text":f"*📋 SALES BRIEF*\n```{brief_text[:2900]}```"}})
        if transcript:
            tx_lines = [f"{'🤖' if t.get('role')=='agent' else '👤'} {t.get('message','')[:120]}"
                        for t in transcript[-8:]]
            tx_preview = "\n".join(tx_lines)
            blocks.append({"type":"divider"})
            blocks.append({"type":"section","text":{"type":"mrkdwn",
                "text":f"*📝 TRANSCRIPT (last {min(8,len(transcript))} turns)*\n```{tx_preview[:2900]}```"}})
    else:
        blocks = [
            {"type":"section","fields":[
                {"type":"mrkdwn","text":f"*{label}*"},
                {"type":"mrkdwn","text":f"{req.prospect_name or '?'} @ {req.company_name or '?'}"},
            ]},
            {"type":"section","text":{"type":"mrkdwn","text":f"> {req.message or ''}"}},
        ]

    if convo_url:
        blocks.append({"type":"actions","elements":[{
            "type":"button","style":"primary","url":convo_url,
            "text":{"type":"plain_text","text":"View Conversation","emoji":True}}]})

    try:
        import httpx
        fallback = f"🔥 {req.prospect_name or 'Unknown'} @ {req.company_name or 'Unknown'} — {label}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(SLACK_WEBHOOK_URL, json={"text": fallback, "blocks": blocks})
            resp.raise_for_status()
    except Exception as e:
        print(f"[slack] error: {e}")



# ─── Live transcript polling ──────────────────────────────────────────────────

class SessionRequest(BaseModel):
    conversation_id: str = ""

@app.post("/demo/session")
async def register_session(req: SessionRequest):
    global _active_conv_id, _live_transcript, _transcript_task
    conv_id = req.conversation_id.strip()
    if not conv_id or conv_id == _active_conv_id:
        return {"ok": True, "detail": "no change"}
    _active_conv_id = conv_id
    _live_transcript = []
    _handoff_state.clear()
    if _transcript_task and not _transcript_task.done():
        _transcript_task.cancel()
    _transcript_task = asyncio.create_task(_poll_transcript(conv_id))
    print(f"[transcript] Started polling for {conv_id}")
    return {"ok": True, "conversation_id": conv_id}

async def _poll_transcript(conversation_id: str):
    global _live_transcript
    if not ELEVENLABS_API_KEY:
        print("[transcript] No ELEVENLABS_API_KEY")
        return
    import httpx
    url = f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    try:
        for _ in range(400):
            await asyncio.sleep(3)
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                turns = data.get("transcript", [])
                if turns:
                    _live_transcript = turns
                if data.get("status") == "done":
                    print(f"[transcript] Conv {conversation_id} done")
                    break
            else:
                print(f"[transcript] {resp.status_code} for {conversation_id}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[transcript] Error: {e}")

# ─── Demo state API ───────────────────────────────────────────────────────────

@app.get("/demo/state")
async def demo_state():
    return {"jobs": _demo_jobs, "signals": _demo_signals, "transcript": _live_transcript}


@app.get("/leads")
async def list_leads(limit: int = 100):
    """The captured partner list — for follow-up and engagement BI."""
    if not DATABASE_URL:
        return {"leads": [], "detail": "no database configured"}
    try:
        pool = await get_pool()
        rows = await pool.fetch(
            """SELECT session_id, partner_name, partner_company, partner_email, partner_phone,
                      customer_vertical, last_signal, signal_count, handed_off,
                      first_seen, last_seen
               FROM partner_leads ORDER BY last_seen DESC LIMIT $1""",
            limit)
        return {"count": len(rows), "leads": [dict(r) for r in rows]}
    except Exception as e:
        return {"leads": [], "error": str(e)}

@app.get("/demo/reset")
async def demo_reset():
    global _active_conv_id, _live_transcript, _transcript_task
    _demo_jobs.clear()
    _demo_signals.clear()
    _live_transcript.clear()
    _handoff_state.clear()
    _active_conv_id = ""
    if _transcript_task and not _transcript_task.done():
        _transcript_task.cancel()
    return {"ok": True}


# ─── HTML Pages ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Guardz — Sales Intelligence</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  html,body {{ height:100%; background:#080818; color:#fff;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif }}
  body {{ display:flex; flex-direction:column; align-items:center; justify-content:center; gap:0 }}

  .hero {{ text-align:center; padding:0 24px }}
  .logo {{ font-size:15px; font-weight:600; letter-spacing:.15em; text-transform:uppercase;
    color:#7c3aed; margin-bottom:28px }}
  h1 {{ font-size:clamp(36px,5vw,64px); font-weight:800; letter-spacing:-2px; line-height:1.1;
    background:linear-gradient(135deg,#fff 40%,#7c3aed); -webkit-background-clip:text;
    -webkit-text-fill-color:transparent; margin-bottom:16px }}
  .sub {{ font-size:16px; color:#6b7280; max-width:420px; line-height:1.6; margin-bottom:48px }}

  .launch-btn {{
    display:inline-flex; align-items:center; gap:10px;
    background:linear-gradient(135deg,#7c3aed,#4f46e5);
    color:#fff; border:none; border-radius:14px; padding:18px 40px;
    font-size:17px; font-weight:700; cursor:pointer; letter-spacing:.01em;
    box-shadow:0 0 40px rgba(124,58,237,.4);
    transition:transform .15s,box-shadow .15s
  }}
  .launch-btn:hover {{ transform:translateY(-2px); box-shadow:0 0 60px rgba(124,58,237,.6) }}
  .launch-btn svg {{ width:20px; height:20px }}

  .pills {{ display:flex; gap:10px; flex-wrap:wrap; justify-content:center; margin-top:40px }}
  .pill {{ background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.1);
    border-radius:20px; padding:6px 14px; font-size:12px; color:#9ca3af }}
</style>
</head>
<body>
<div class="hero">
  <div class="logo">Guardz</div>
  <h1>AI Sales Intelligence<br>in Real Time</h1>
  <p class="sub">Watch the agent research your prospect, detect buying signals, and brief your team — live, as the conversation unfolds.</p>
  <button class="launch-btn" onclick="window.location='/demo/live'">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>
    Launch Demo
  </button>
  <div class="pills">
    <span class="pill">Live company research</span>
    <span class="pill">Real-time signals</span>
    <span class="pill">Auto transfer to human</span>
  </div>
</div>
</body>
</html>""")


@app.get("/demo/live", response_class=HTMLResponse)
async def demo_live():
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Guardz — Live Demo</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  html,body {{ height:100%; background:#080818; color:#fff;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; overflow:hidden }}

  /* ── Top bar ── */
  header {{
    height:52px; background:#0d0d24; border-bottom:1px solid rgba(255,255,255,.07);
    display:flex; align-items:center; justify-content:space-between; padding:0 20px; flex-shrink:0
  }}
  .hlogo {{ font-size:14px; font-weight:700; color:#a78bfa; letter-spacing:.1em; text-transform:uppercase }}
  .hright {{ display:flex; align-items:center; gap:16px }}
  .badge {{ font-size:11px; padding:3px 10px; border-radius:20px; font-weight:600 }}
  .badge.live {{ background:rgba(16,185,129,.15); color:#10b981; border:1px solid rgba(16,185,129,.3) }}
  .reset-btn {{ font-size:11px; color:#6b7280; background:none; border:1px solid rgba(255,255,255,.1);
    border-radius:6px; padding:4px 10px; cursor:pointer }}
  .reset-btn:hover {{ color:#fff; border-color:rgba(255,255,255,.3) }}

  /* ── 3 panels ── */
  .panels {{
    display:grid; grid-template-columns:1fr 1fr 1fr;
    height:calc(100vh - 52px); gap:1px; background:rgba(255,255,255,.05)
  }}
  .panel {{
    background:#0d0d24; display:flex; flex-direction:column; overflow:hidden
  }}
  .panel-head {{
    padding:16px 20px 12px; border-bottom:1px solid rgba(255,255,255,.06);
    flex-shrink:0
  }}
  .panel-title {{ font-size:11px; font-weight:700; letter-spacing:.12em;
    text-transform:uppercase; color:#6b7280; margin-bottom:4px }}
  .panel-status {{ font-size:13px; font-weight:600; color:#e2e8f0 }}
  .panel-body {{ flex:1; overflow-y:auto; padding:20px }}
  .panel-body::-webkit-scrollbar {{ width:4px }}
  .panel-body::-webkit-scrollbar-track {{ background:transparent }}
  .panel-body::-webkit-scrollbar-thumb {{ background:rgba(255,255,255,.1); border-radius:2px }}

  /* ── Chat panel ── */
  .chat-hint {{
    background:rgba(124,58,237,.1); border:1px solid rgba(124,58,237,.25);
    border-radius:12px; padding:18px; margin-bottom:16px
  }}
  .chat-hint p {{ font-size:13px; color:#c4b5fd; line-height:1.6 }}
  .chat-hint strong {{ color:#a78bfa }}
  .step {{ display:flex; gap:12px; align-items:flex-start; margin-bottom:12px }}
  .step-num {{ width:22px; height:22px; border-radius:50%; background:#7c3aed;
    font-size:11px; font-weight:700; display:flex; align-items:center; justify-content:center;
    flex-shrink:0; margin-top:1px }}
  .step-text {{ font-size:13px; color:#9ca3af; line-height:1.5 }}

  /* ── Research panel ── */
  .waiting {{ text-align:center; padding:40px 0 }}
  .waiting-icon {{ font-size:32px; margin-bottom:12px }}
  .waiting-text {{ font-size:13px; color:#4b5563 }}

  .pulse {{ display:inline-block; width:8px; height:8px; border-radius:50%;
    background:#7c3aed; margin-right:8px;
    animation:pulse 1.5s ease-in-out infinite }}
  @keyframes pulse {{ 0%,100%{{opacity:1;transform:scale(1)}} 50%{{opacity:.4;transform:scale(.8)}} }}

  .section {{ margin-bottom:20px }}
  .section-label {{ font-size:10px; font-weight:700; letter-spacing:.1em; text-transform:uppercase;
    color:#4b5563; margin-bottom:10px }}
  .company-name {{ font-size:20px; font-weight:700; color:#f1f5f9; margin-bottom:4px }}
  .company-meta {{ font-size:13px; color:#6b7280; line-height:1.7 }}
  .conf-badge {{ display:inline-block; padding:2px 10px; border-radius:20px;
    font-size:11px; font-weight:700; margin-bottom:14px }}
  .conf-high {{ background:rgba(16,185,129,.15); color:#10b981; border:1px solid rgba(16,185,129,.3) }}
  .conf-medium {{ background:rgba(245,158,11,.15); color:#f59e0b; border:1px solid rgba(245,158,11,.3) }}
  .conf-low {{ background:rgba(239,68,68,.1); color:#f87171; border:1px solid rgba(239,68,68,.2) }}
  .tag {{ display:inline-block; background:rgba(124,58,237,.15); color:#c4b5fd;
    border:1px solid rgba(124,58,237,.2); border-radius:6px;
    padding:4px 10px; font-size:12px; margin:3px 3px 3px 0 }}
  .context-note {{ background:rgba(79,70,229,.1); border-left:3px solid #4f46e5;
    border-radius:0 8px 8px 0; padding:12px 14px; font-size:13px;
    color:#a5b4fc; line-height:1.6; font-style:italic }}
  .duration {{ font-size:11px; color:#374151; margin-top:16px }}

  /* ── Signals panel ── */
  .signal-card {{
    background:rgba(255,255,255,.03); border:1px solid rgba(255,255,255,.07);
    border-radius:10px; padding:14px 16px; margin-bottom:12px;
    animation:slidein .3s ease
  }}
  @keyframes slidein {{ from{{opacity:0;transform:translateY(-8px)}} to{{opacity:1;transform:none}} }}
  .signal-label {{ font-size:13px; font-weight:600; color:#f1f5f9; margin-bottom:6px }}
  .signal-who {{ font-size:12px; color:#6b7280; margin-bottom:8px }}
  .signal-quote {{ font-size:12px; color:#9ca3af; font-style:italic;
    border-left:2px solid #7c3aed; padding-left:10px; line-height:1.5 }}
  .signal-time {{ font-size:10px; color:#374151; margin-top:8px; text-align:right }}
  .no-signals {{ text-align:center; padding:40px 0 }}
  .no-signals-icon {{ font-size:28px; margin-bottom:10px }}
  .no-signals-text {{ font-size:12px; color:#374151 }}
  .transfer-alert {{
    background:rgba(16,185,129,.1); border:1px solid rgba(16,185,129,.3);
    border-radius:10px; padding:14px 16px; margin-bottom:12px;
    animation:slidein .3s ease
  }}
  .transfer-alert .signal-label {{ color:#10b981 }}

  /* ── Transcript panel ── */
  .tx-turn {{ margin-bottom:12px; animation:slidein .25s ease }}
  .tx-turn-agent {{ padding-left:0 }}
  .tx-turn-user  {{ padding-left:0 }}
  .tx-label {{ font-size:10px; font-weight:700; letter-spacing:.08em;
    text-transform:uppercase; margin-bottom:4px }}
  .tx-label-agent {{ color:#7c3aed }}
  .tx-label-user  {{ color:#0ea5e9 }}
  .tx-bubble {{ font-size:13px; line-height:1.6; padding:10px 14px;
    border-radius:10px; word-break:break-word }}
  .tx-bubble-agent {{ background:rgba(124,58,237,.1); color:#ddd6fe;
    border:1px solid rgba(124,58,237,.2) }}
  .tx-bubble-user  {{ background:rgba(14,165,233,.08); color:#bae6fd;
    border:1px solid rgba(14,165,233,.15) }}
  .tx-signal {{ background:rgba(16,185,129,.08); border:1px solid rgba(16,185,129,.25);
    border-radius:8px; padding:8px 12px; margin:10px 0;
    font-size:12px; color:#6ee7b7; line-height:1.5 }}
  .tx-signal-icon {{ margin-right:6px }}
  .tx-empty {{ text-align:center; padding:40px 0 }}
  .tx-empty-icon {{ font-size:28px; margin-bottom:10px }}
  .tx-empty-text {{ font-size:12px; color:#374151 }}
</style>
</head>
<body>

<header>
  <span class="hlogo">Guardz</span>
  <div class="hright">
    <span class="badge live">● LIVE</span>
    <button class="reset-btn" onclick="resetDemo()">Reset Demo</button>
    <a href="/" style="font-size:11px;color:#6b7280;text-decoration:none">← Home</a>
  </div>
</header>

<div class="panels">

  <!-- Panel 1: Caller Background -->
  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">Panel 1</div>
      <div class="panel-status">🎭 Caller Background</div>
    </div>
    <div class="panel-body" style="display:flex;flex-direction:column;gap:0">

      <div id="bg-card" style="flex:1">
        <div style="text-align:center;color:#4b5563;font-size:13px;padding:40px 0">
          Click the button below to get your caller identity.
        </div>
      </div>

      <button onclick="newBackground()" style="
        margin-top:auto;width:100%;padding:12px;
        background:linear-gradient(135deg,#6366f1,#8b5cf6);
        border:none;border-radius:8px;color:#fff;
        font-size:13px;font-weight:700;letter-spacing:.04em;
        cursor:pointer;transition:opacity .15s
      " onmouseover="this.style.opacity='.85'" onmouseout="this.style.opacity='1'">
        🎲 Click for Background
      </button>

      <div id="chat-status" style="font-size:11px;color:#4b5563;text-align:center;padding:6px 0">
        Waiting for conversation to start…
      </div>
    </div>
  </div>

  <script>
  const BG_POOL = [
    {{ name:"Mike Torres", company:"Corsica Technologies", city:"Kent, WA", email:"mike@corsicatech.com",
      selling:"Dental offices — 8 clients asking about cyber insurance compliance",
      questions:["Does Guardz satisfy what cyber insurers are now requiring — EDR, email security, all of it?","Can I show a prospect their risk score before they sign with me?"] }},
    {{ name:"Sarah Kim", company:"Charles IT", city:"Orange, CT", email:"sarah@charlesit.com",
      selling:"Law firms — worried about client data breach liability",
      questions:["Does Guardz cover identity threats and email together in one platform?","What does onboarding look like for a 15-seat law firm?"] }},
    {{ name:"Dave Okonkwo", company:"Centre Technologies", city:"Houston, TX", email:"dave@centretechnologies.com",
      selling:"CPA firms — clients asking about financial data protection",
      questions:["My clients are mostly on Microsoft 365 — does Guardz handle M365 email security?","What's the margin like for a partner my size?"] }},
    {{ name:"Jennifer Walsh", company:"Omega Systems", city:"West Chester, PA", email:"jen@omegasystemsinc.com",
      selling:"Real estate agencies — handling sensitive client financial data",
      questions:["Does Guardz include security awareness training and phishing simulation?","I'm not a security expert — how much do I need to know to sell this?"] }},
    {{ name:"Carlos Rivera", company:"eMazzanti Technologies", city:"Hoboken, NJ", email:"carlos@emazzanti.net",
      selling:"Small manufacturers — hit by ransomware last year, now paranoid",
      questions:["What does the MDR piece actually mean — who's watching alerts at 2am?","Can Guardz prevent ransomware, or just detect it after the fact?"] }},
    {{ name:"Amanda Chu", company:"Executech", city:"Salt Lake City, UT", email:"amanda@executech.com",
      selling:"Healthcare clinics — HIPAA compliance pressure increasing",
      questions:["Does Guardz help clients meet HIPAA security requirements?","How does the cyber insurance piece work — do you broker it or just help qualify?"] }},
    {{ name:"Brian Murphy", company:"Mainstay Technologies", city:"Manchester, NH", email:"brian@mainstaytechnologies.com",
      selling:"Financial services firms — regulators asking hard questions",
      questions:["I'm losing deals to bigger MSPs with managed security. Can Guardz close that gap?","What's the partner program structure — tiers, margins, support?"] }},
    {{ name:"Lisa Patel", company:"XPERTECHS", city:"Hunt Valley, MD", email:"lisa@xpertechs.com",
      selling:"Construction companies — new to security, don't know where to start",
      questions:["Can I manage all my clients from one dashboard?","How fast can I actually get a client up and running?"] }},
    {{ name:"Tom Nguyen", company:"NexusTek", city:"Denver, CO", email:"tom@nexustek.com",
      selling:"Accounting firms — clients storing sensitive tax data",
      questions:["One of my clients just got a ransomware demand — could Guardz have prevented that?","Does Guardz work alongside existing tools or does it replace everything?"] }},
    {{ name:"Rachel Stevens", company:"Kelser Corporation", city:"Glastonbury, CT", email:"rachel@kelserinc.com",
      selling:"Medical practices — patients asking about data security after hospital breaches in the news",
      questions:["Does Guardz cover identity threat detection — like compromised employee accounts?","What's the pricing — per user, per client, flat rate?"] }},
    {{ name:"Kevin Park", company:"Valiant Technology", city:"New York, NY", email:"kevin@valianttechnology.com",
      selling:"Startups and small tech companies — SOC 2 compliance coming up",
      questions:["Can Guardz help clients build the evidence they need for SOC 2 or cyber insurance audits?","Is there a free tier I can use to show a client before committing?"] }},
    {{ name:"Greg Henderson", company:"VC3", city:"Columbia, SC", email:"greg@vc3.com",
      selling:"Insurance agencies — ironic that they have no security themselves",
      questions:["How does the external footprint scanning work — does it find things clients don't know are exposed?","What kind of reporting can I give a client after a scan?"] }},
    {{ name:"Nikki Brown", company:"Integris", city:"Oklahoma City, OK", email:"nikki@integrisit.com",
      selling:"Architecture and engineering firms — lots of intellectual property to protect",
      questions:["Does Guardz protect against insider threats or just external attacks?","I currently use a patchwork of tools — what does Guardz actually replace?"] }},
    {{ name:"James Wilson", company:"Complete Network", city:"Charlotte, NC", email:"james@completenetwork.com",
      selling:"Dental group practices — multiple locations, one IT partner",
      questions:["Can I run one Guardz deployment across multiple client locations?","My clients keep asking me about cyber insurance — can Guardz help them qualify?"] }},
    {{ name:"Maria Santos", company:"Dataprise", city:"Rockville, MD", email:"maria@dataprise.com",
      selling:"Retail businesses — POS systems and customer payment data",
      questions:["Does Guardz cover endpoint security or is it just monitoring?","I have clients who think they're too small to be targeted — how do I make the case?"] }},
  ];

  let lastIdx = -1;

  function newBackground() {{
    let idx;
    do {{ idx = Math.floor(Math.random() * BG_POOL.length); }} while (idx === lastIdx && BG_POOL.length > 1);
    lastIdx = idx;
    const p = BG_POOL[idx];
    document.getElementById('bg-card').innerHTML = `
      <div style="margin-bottom:14px">
        <div style="font-size:17px;font-weight:700;color:#f1f5f9">${{p.name}}</div>
        <div style="font-size:12px;color:#6b7280;margin-top:2px">${{p.company}} &middot; ${{p.city}}</div>
        <div style="font-size:11px;color:#4b5563;margin-top:4px;font-family:monospace;background:rgba(255,255,255,.04);padding:3px 8px;border-radius:4px;display:inline-block">${{p.email}}</div>
      </div>
      <div class="section">
        <div class="section-label">Selling Into</div>
        <div style="font-size:12px;color:#94a3b8;line-height:1.6">${{p.selling}}</div>
      </div>
      <div class="section">
        <div class="section-label">Ask These Questions</div>
        ${{p.questions.map(q => `<div style="font-size:12px;color:#94a3b8;line-height:1.6;padding:5px 0;border-bottom:1px solid rgba(255,255,255,.05)">&rsaquo; ${{q}}</div>`).join('')}}
      </div>
    `;
  }}
  </script>

  <!-- Panel 2: Research -->
  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">Panel 2</div>
      <div class="panel-status" id="research-status">🔬 Background Research</div>
    </div>
    <div class="panel-body" id="research-body">
      <div class="waiting">
        <div class="waiting-icon">🔬</div>
        <div class="waiting-text">Research starts automatically<br>once the agent collects your email</div>
      </div>
    </div>
  </div>

  <!-- Panel 3: Live Transcript -->
  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">Panel 3</div>
      <div class="panel-status" id="transcript-status">📝 Live Transcript</div>
    </div>
    <div class="panel-body" id="transcript-body">
      <div class="tx-empty">
        <div class="tx-empty-icon">📝</div>
        <div class="tx-empty-text">Transcript builds here<br>as the conversation unfolds</div>
      </div>
    </div>
  </div>

</div>

<!-- Guardz Sales Agent Widget -->
<elevenlabs-convai agent-id="{ELEVENLABS_AGENT_ID}"></elevenlabs-convai>
<script src="https://elevenlabs.io/convai-widget/index.js" async type="text/javascript"></script>
<script>
// Capture conversation_id from widget as soon as call connects
(function() {{
  function tryAttach() {{
    const w = document.querySelector('elevenlabs-convai');
    if (!w) {{ setTimeout(tryAttach, 500); return; }}
    // ElevenLabs widget fires these events on the element
    ['elevenlabs-convai:connect','elevenlabs-convai:call_started','connect'].forEach(evt => {{
      w.addEventListener(evt, function(e) {{
        const cid = (e.detail && (e.detail.conversation_id || e.detail.conversationId)) || '';
        if (cid) {{
          console.log('[transcript] conversation_id:', cid);
          fetch('/demo/session', {{
            method: 'POST',
            headers: {{'Content-Type':'application/json'}},
            body: JSON.stringify({{conversation_id: cid}})
          }});
        }}
      }});
    }});
  }}
  tryAttach();
}})();
</script>

<script>
let lastJobStatus = null;
let lastSignalCount = 0;
let pollingActive = false;

function fmtTime(iso) {{
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString([], {{hour:'2-digit',minute:'2-digit',second:'2-digit'}});
}}

function confClass(c) {{
  if (c === 'high') return 'conf-high';
  if (c === 'medium') return 'conf-medium';
  return 'conf-low';
}}

function renderResearch(job) {{
  const status = document.getElementById('research-status');
  const body = document.getElementById('research-body');
  const chatStatus = document.getElementById('chat-status');

  if (job.status === 'running') {{
    status.innerHTML = '<span class="pulse"></span> Researching ' + (job.domain || '…');
    body.innerHTML = `
      <div style="padding:20px 0">
        <div style="font-size:13px;color:#7c3aed;margin-bottom:16px">
          <span class="pulse"></span>Running parallel web searches for <strong>${{job.domain}}</strong>…
        </div>
        <div style="display:flex;flex-direction:column;gap:8px">
          ${{['Company overview','LinkedIn profile','Recent news','Industry benchmarks','Tech stack signals'].map(q =>
            `<div style="background:rgba(124,58,237,.08);border:1px solid rgba(124,58,237,.15);border-radius:8px;padding:10px 14px;font-size:12px;color:#7c3aed">
              <span class="pulse"></span>${{q}}
            </div>`
          ).join('')}}
        </div>
      </div>`;
    chatStatus.textContent = 'Research running for ' + job.domain + '…';
    return;
  }}

  if (job.status === 'done') {{
    const c = job.company || {{}};
    const sc = job.sales_context || {{}};
    const conf = c.confidence || sc.confidence || 'low';
    const pains = sc.pain_points || [];
    const triggers = sc.buying_triggers || [];
    const objections = sc.common_objections || [];
    const trends = sc.industry_trends || [];
    const tech = c.tech_stack_signals || [];
    const comps = c.key_competitors || [];
    const news = c.recent_news || [];
    const notes = c.research_notes || '';

    status.textContent = '✅ Research Complete — ' + (c.company_name || job.domain);
    chatStatus.textContent = 'Research done for ' + job.domain;

    body.innerHTML = `
      <div class="section">
        <span class="conf-badge ${{confClass(conf)}}">${{conf.toUpperCase()}} CONFIDENCE</span>
        <div class="company-name">${{c.company_name || job.domain}}</div>
        <div class="company-meta">
          ${{[c.industry, c.sub_industry].filter(Boolean).join(' › ')}}<br>
          ${{[c.company_size ? c.company_size+' employees' : '', c.hq_location, c.funding_stage].filter(Boolean).join(' · ')}}
        </div>
        ${{c.description ? `<p style="font-size:13px;color:#6b7280;margin-top:10px;line-height:1.6">${{c.description}}</p>` : ''}}
      </div>

      ${{tech.length ? `
      <div class="section">
        <div class="section-label">Technology Stack</div>
        ${{tech.map(t => `<span class="tag" style="background:rgba(99,102,241,.08);color:#818cf8;border-color:rgba(99,102,241,.2)">${{t}}</span>`).join('')}}
      </div>` : ''}}

      ${{comps.length ? `
      <div class="section">
        <div class="section-label">Key Competitors</div>
        ${{comps.map(x => `<span class="tag" style="background:rgba(107,114,128,.08);color:#9ca3af;border-color:rgba(107,114,128,.2)">${{x}}</span>`).join('')}}
      </div>` : ''}}

      ${{notes ? `
      <div class="section">
        <div class="section-label">Partner Programs & Certifications</div>
        <div style="font-size:12px;color:#94a3b8;line-height:1.7">${{notes}}</div>
      </div>` : ''}}

      ${{news.length ? `
      <div class="section">
        <div class="section-label">Recent News</div>
        ${{news.slice(0,3).map(n => `<div style="font-size:12px;color:#6b7280;padding:5px 0;border-bottom:1px solid rgba(255,255,255,.05);line-height:1.5">▸ ${{n}}</div>`).join('')}}
      </div>` : ''}}

      ${{pains.length ? `
      <div class="section">
        <div class="section-label">Top Pain Points</div>
        ${{pains.map(p => `<span class="tag">${{p}}</span>`).join('')}}
      </div>` : ''}}

      ${{triggers.length ? `
      <div class="section">
        <div class="section-label">Buying Triggers</div>
        ${{triggers.map(t => `<span class="tag" style="background:rgba(16,185,129,.1);color:#10b981;border-color:rgba(16,185,129,.2)">${{t}}</span>`).join('')}}
      </div>` : ''}}

      ${{objections.length ? `
      <div class="section">
        <div class="section-label">Common Objections</div>
        ${{objections.map(o => `<span class="tag" style="background:rgba(239,68,68,.08);color:#f87171;border-color:rgba(239,68,68,.2)">${{o}}</span>`).join('')}}
      </div>` : ''}}

      ${{trends.length ? `
      <div class="section">
        <div class="section-label">Industry Trends</div>
        ${{trends.map(t => `<span class="tag" style="background:rgba(245,158,11,.08);color:#f59e0b;border-color:rgba(245,158,11,.2)">${{t}}</span>`).join('')}}
      </div>` : ''}}

      ${{sc.context_note ? `
      <div class="section">
        <div class="section-label">Agent Context Note</div>
        <div class="context-note">${{sc.context_note}}</div>
      </div>` : ''}}

      ${{job.duration_seconds ? `<div class="duration">Research completed in ${{job.duration_seconds}}s</div>` : ''}}
    `;
    return;
  }}

  if (job.status === 'failed') {{
    status.textContent = '❌ Research Failed';
    body.innerHTML = `<div style="color:#f87171;font-size:13px;padding:20px 0">${{job.error || 'Unknown error'}}</div>`;
  }}
}}

function renderPanel3(transcript, signals) {{
  const body = document.getElementById('transcript-body');
  const status = document.getElementById('transcript-status');
  if (!body) return;

  const hasTx = transcript && transcript.length > 0;
  const hasSig = signals && signals.length > 0;

  if (!hasTx && !hasSig) return;

  // Map signals to a simple list for callout rendering
  const sigList = signals || [];

  let html = '';

  if (hasTx) {{
    status.textContent = '📝 Live Transcript — ' + transcript.length + ' turns';
    html += transcript.map(turn => {{
      const isAgent = turn.role === 'agent';
      const label = isAgent ? '🤖 Guardz Agent' : '👤 Caller';
      const cls = isAgent ? 'agent' : 'user';
      return `<div class="tx-turn tx-turn-${{cls}}">
        <div class="tx-label tx-label-${{cls}}">${{label}}</div>
        <div class="tx-bubble tx-bubble-${{cls}}">${{turn.message || ''}}</div>
      </div>`;
    }}).join('');
  }}

  if (hasSig) {{
    html += sigList.map(s => `
      <div class="tx-signal">
        <span class="tx-signal-icon">🔔</span>
        <strong>${{s.label || s.signal_type}}</strong>
        ${{s.message ? ` — "${{s.message}}"` : ''}}
        <span style="float:right;color:#4b5563;font-size:10px">${{fmtTime(s.timestamp)}}</span>
      </div>`).join('');
  }}

  body.innerHTML = html;
  body.scrollTop = body.scrollHeight;
}}

function renderSignals(signals) {{
  // Kept for compatibility — now routed through renderPanel3
}}

async function poll() {{
  try {{
    const r = await fetch('/demo/state');
    const data = await r.json();
    if (data.jobs && data.jobs.length > 0) {{
      renderResearch(data.jobs[0]);
    }}
    const transcript = data.transcript || [];
    const signals = data.signals || [];
    if (transcript.length > 0 || signals.length > lastSignalCount) {{
      lastSignalCount = signals.length;
      renderPanel3(transcript, signals);
    }}
  }} catch(e) {{ /* ignore */ }}
}}

async function resetDemo() {{
  await fetch('/demo/reset');
  lastJobStatus = null;
  lastSignalCount = 0;
  document.getElementById('research-status').textContent = '🔬 Background Research';
  document.getElementById('research-body').innerHTML = `
    <div class="waiting">
      <div class="waiting-icon">🔬</div>
      <div class="waiting-text">Research starts automatically<br>once the agent collects your email</div>
    </div>`;
  document.getElementById('transcript-body').innerHTML = `
    <div class="tx-empty">
      <div class="tx-empty-icon">📝</div>
      <div class="tx-empty-text">Transcript builds here<br>as the conversation unfolds</div>
    </div>`;
  if (document.getElementById('transcript-status'))
    document.getElementById('transcript-status').textContent = '📝 Live Transcript';
  document.getElementById('chat-status').textContent = 'Waiting for conversation to start…';
}}

setInterval(poll, 2000);
</script>
</body>
</html>""")
