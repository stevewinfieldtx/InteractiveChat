"""
api.py — Guardz Research Agent + Live Demo
(PATCHED: CPP Voice v3 injected into all LLM generation prompts)
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
from cpp_voice import CPP_VOICE, CPP_STYLE_LIGHT

app = FastAPI(title="Guardz Research Agent", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATABASE_URL = os.getenv("DATABASE_URL", "")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "agent_5101kty2ztmme25aspqycwp7mpsm")
ELEVENLABS_API_KEY  = os.getenv("VITE_ELEVENLABS_API_KEY", os.getenv("ELEVENLABS_API_KEY", ""))
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL    = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4-5")

# ─── Email handoff config ─────────────────────────────────────────────────────
SALES_TEAM_EMAIL = os.getenv("SALES_TEAM_EMAIL", "stevewinfieldtx@gmail.com")
SALES_REP_NAME   = os.getenv("SALES_REP_NAME", "Steve")
EMAIL_FROM       = os.getenv("EMAIL_FROM", "Rain Networks <onboarding@resend.dev>")
RESEND_API_KEY   = os.getenv("RESEND_API_KEY", "")
SMTP_HOST        = os.getenv("SMTP_HOST", "")
SMTP_PORT        = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER        = os.getenv("SMTP_USER", "")
SMTP_PASS        = os.getenv("SMTP_PASS", "")
SEND_AVAILABILITY_PING = os.getenv("HANDOFF_SEND_AVAILABILITY", "true").lower() == "true"
GUARDZ_HUMAN_WAIT_SECONDS = int(os.getenv("GUARDZ_HUMAN_WAIT_SECONDS", "60"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://interactivechat.up.railway.app")

# ─── Demo state (in-memory, resets on redeploy) ───────────────────────────────

_demo_jobs: list[dict] = []
_demo_signals: list[dict] = []
_live_transcript: list[dict] = []
_active_conv_id: str = ""
_transcript_task: asyncio.Task | None = None
_handoff_state: dict[str, dict] = {}
_chats: dict[str, dict] = {}

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

    _upsert_demo_job(
        req.session_id,
        domain=domain,
        status="running",
        started_at=datetime.utcnow().isoformat(),
        company=None, industry=None, sales_context=None,
    )

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
    contact_email: str = ""
    contact_phone: str = ""

class NotifyHumanResponse(BaseModel):
    ok: bool
    available: bool = False
    rep_name: str = ""
    detail: str = ""


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CPP-PATCHED: _generate_sales_brief — uses CPP_STYLE_LIGHT                ║
# ║  Brief is FOR Steve to read, so match his preferred reading style.         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

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

{CPP_STYLE_LIGHT}

Use ONLY what the PARTNER actually said (the lines marked PARTNER below). Never credit them with a topic just because the AGENT raised it. If they did not actually express a given concern or interest, do not invent it. Say it's not yet known. Quote the partner's own words where you can.

PARTNER ({SALES_REP_NAME} is calling them back): {partner_name or "Unknown"} at {partner_company or "Unknown"}

THEIR TARGET CUSTOMER MARKET (what Guardz must win for them):
{customer_ctx or "(not yet identified -- flag pinning this down as the first move)"}

TRANSCRIPT:
{tx_text}

Respond in exactly this format (keep each section tight):

HIGHLIGHTS
- [most important thing revealed]
- [second most important]
- [third -- only if genuinely distinct]

INTENT SCORE
[number 0-100]% -- [one sentence: what signals drove this score]

DIRECTION
[1-2 sentences: where the partner is in the journey and what they're about to do]

BIGGEST CONCERN
[The single clearest objection or blocker -- quote their words if possible]

QUESTIONS FOR {SALES_REP_NAME.upper()}
1. [Exact question to ask the partner] -- WHY: [what this unlocks]
2. [Exact question to ask the partner] -- WHY: [what this unlocks]
3. [Exact question to ask the partner] -- WHY: [what this unlocks]"""

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
    print(f"[email] No provider configured -- would send to {to}: {subject}")
    return False, "no email provider configured (set RESEND_API_KEY or SMTP_HOST/USER/PASS)"


def _brief_to_html(brief_text: str) -> str:
    import html as _h
    if not brief_text:
        return "<p style='color:#9ca3af;font-size:14px'>(AI brief unavailable -- check OPENROUTER_API_KEY)</p>"
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
        return "<p style='color:#9ca3af;font-size:14px'>(not identified on the call -- first thing to pin down)</p>"
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
    subject = f"⚡ Partner ready for a call -- are you free? ({contact.get('name') or 'Unknown'} @ {contact.get('company') or 'Unknown'})"
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
          f"<b>{_h.escape(SALES_REP_NAME)}</b> -- if you can take this, the AI is collecting the partner's callback number now. "
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
    subject = f"🔥 Call now: {name} @ {company} -- callback {phone}"
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
        f"<div style='font-size:20px;font-weight:800;margin-top:6px'>{_h.escape(SALES_REP_NAME)}, you're up -- call this partner now</div></div>"
        "<div style='padding:8px 24px 24px'>"
        + _section("Contact (call the partner)", contact_html)
        + _section("Customer / target vertical -- the real target", _customer_to_html(customer))
        + _section("What triggered the handoff", trigger_html)
        + _section("AI Sales Brief", _brief_to_html(brief_text))
        + _section(f"Transcript -- last {len(transcript or [])} turns", _transcript_to_html(transcript))
        + "</div>"
    )
    return subject, _email_shell(inner)


# ─── Notify human (background-driven email + optional Slack) ───────────────────

HANDOFF_SIGNALS = ("handoff_requested", "strong_interest", "demo_agreed",
                   "named_client", "pricing_question", "how_to_start")


async def _save_lead(*, session_id, name, company, email, phone, vertical,
                     signal, handed_off, brief, transcript):
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

    asyncio.create_task(_process_notification(req, label))

    return NotifyHumanResponse(ok=True, available=True, rep_name=SALES_REP_NAME, detail="accepted")


async def _process_notification(req: NotifyHumanRequest, label: str):
    try:
        is_handoff = req.signal_type in HANDOFF_SIGNALS
        transcript = list(_live_transcript)
        job = _demo_jobs[0] if _demo_jobs else {}
        customer_co = job.get("company") or {}
        sales_context = job.get("sales_context") or {}

        recent_user_text = " ".join(
            t.get("message", "") for t in transcript[-6:] if t.get("role") != "agent"
        )
        contact = {
            "name": req.prospect_name or "",
            "company": req.company_name or "",
            "email": _extract_email(req.contact_email, req.message, _transcript_text(transcript)),
            "phone": _extract_phone(req.contact_phone, req.message, recent_user_text),
        }
        customer = {
            "name": customer_co.get("company_name") or sales_context.get("industry") or "",
            "industry": customer_co.get("industry") or sales_context.get("industry") or "",
            "pains": (sales_context.get("pain_points") or [])[:5],
            "triggers": (sales_context.get("buying_triggers") or [])[:5],
            "regulations": (sales_context.get("regulatory_pressures") or [])[:4],
        }
        brief_text = ""

        if is_handoff:
            st = _handoff_state.setdefault(
                req.session_id or "default", {"availability_sent": False, "brief_sent": False})

            if SEND_AVAILABILITY_PING and not st["availability_sent"]:
                subj, html = _build_availability_email(contact, customer, req.message, label)
                ok, detail = await send_email(SALES_TEAM_EMAIL, subj, html)
                st["availability_sent"] = True
                print(f"[handoff] availability email -> {SALES_TEAM_EMAIL}: ok={ok} ({detail})")

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

        if SLACK_WEBHOOK_URL:
            await _post_slack(req, label, is_handoff, customer_co, sales_context, transcript)

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
            {"type":"header","text":{"type":"plain_text","text":f"🔥 HANDOFF -- {SALES_REP_NAME}, you're up","emoji":True}},
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
        fallback = f"🔥 {req.prospect_name or 'Unknown'} @ {req.company_name or 'Unknown'} -- {label}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(SLACK_WEBHOOK_URL, json={"text": fallback, "blocks": blocks})
            resp.raise_for_status()
    except Exception as e:
        print(f"[slack] error: {e}")



# ─── Live transcript (pushed from the browser SDK) ────────────────────────────

class SessionRequest(BaseModel):
    conversation_id: str = ""

class TranscriptTurn(BaseModel):
    role: str = "user"
    message: str = ""

@app.post("/demo/session")
async def register_session(req: SessionRequest):
    global _active_conv_id, _live_transcript, _transcript_task
    conv_id = req.conversation_id.strip()
    if not conv_id or conv_id == _active_conv_id:
        return {"ok": True, "detail": "no change"}
    _active_conv_id = conv_id
    _live_transcript = []
    _handoff_state.clear()
    _demo_jobs.clear()
    _demo_signals.clear()
    if _transcript_task and not _transcript_task.done():
        _transcript_task.cancel()
    print(f"[session] Registered {conv_id} (live transcript via client push)")
    return {"ok": True, "conversation_id": conv_id}

@app.post("/demo/transcript")
async def add_transcript(turn: TranscriptTurn):
    global _live_transcript
    msg = (turn.message or "").strip()
    if msg:
        role = "agent" if turn.role in ("agent", "ai") else "user"
        _live_transcript.append({"role": role, "message": msg})
        if len(_live_transcript) > 200:
            _live_transcript = _live_transcript[-200:]
    return {"ok": True, "turns": len(_live_transcript)}


class CallEndedRequest(BaseModel):
    turns: list[dict] = []


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CPP-PATCHED: _generate_call_summary — uses CPP_STYLE_LIGHT               ║
# ║  Summary is FOR Steve to read. Match his preferred reading style.          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

async def _generate_call_summary(transcript: list, customer: dict, sales_context: dict) -> str:
    """Post-call wrap-up: a tight summary + 3 next-direction questions, from the FULL transcript."""
    if not OPENROUTER_API_KEY or not transcript:
        return ""
    import httpx
    tx = "\n".join(
        f"{'AGENT' if t.get('role') == 'agent' else 'PARTNER'}: {(t.get('message') or '').strip()}"
        for t in transcript
    )
    cu = customer or {}
    ctx = sales_context or {}
    market = cu.get("company_name") or ctx.get("industry") or ""
    pains = ", ".join((ctx.get("pain_points") or [])[:4])
    ctx_line = (f"Partner's customer market: {market}." if market else "")
    if pains:
        ctx_line += f" Known customer pains: {pains}."

    prompt = f"""You are a sales coach at Rain Networks. A rep just finished a call with an IT reseller (the PARTNER) about selling Guardz to the partner's own customers. {ctx_line}

{CPP_STYLE_LIGHT}

Read the transcript (lines marked PARTNER are the human prospect; AGENT is our rep). Use ONLY what was actually said. Do not invent interest or details.

Write the brief in exactly this format:

SUMMARY
3-4 tight sentences: who the partner is, what their customers need, and exactly where this stands now.

NEXT QUESTIONS
Three specific questions the rep should ask on the NEXT call to move this forward. Each on its own line as: "1. <question> -- WHY: <one line>". Make them follow naturally from what was actually discussed. Not generic.

TRANSCRIPT:
{tx}"""
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "HTTP-Referer": "https://interactivechat.up.railway.app",
                         "X-Title": "Rain Networks"},
                json={"model": OPENROUTER_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 600, "temperature": 0.3},
            )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        print(f"[summary] OpenRouter {resp.status_code}: {resp.text[:160]}")
    except Exception as e:
        print(f"[summary] error: {e}")
    return ""


@app.post("/demo/call-ended")
async def call_ended(req: CallEndedRequest):
    transcript = req.turns or list(_live_transcript)
    job = _demo_jobs[0] if _demo_jobs else {}
    summary = await _generate_call_summary(
        transcript, job.get("company") or {}, job.get("sales_context") or {})
    return {"summary": summary or "(summary unavailable -- check OPENROUTER_API_KEY / transcript)"}


# ─── Live chat copilot (human-led chat, AI coaches on a side channel) ──────────

class ChatMessage(BaseModel):
    session_id: str = "copilot"
    role: str = "customer"
    message: str = ""
    simulate: bool = False
    wait: int = 0


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CPP-PATCHED: _generate_copilot — uses CPP_STYLE_LIGHT                    ║
# ║  Coaching tips should match Steve's preferred terse, direct style.         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

async def _generate_copilot(messages: list) -> dict:
    """Score the live chat + coach the rep: health (green/yellow/red), intent, tone, next move."""
    if not OPENROUTER_API_KEY or not messages:
        return {}
    import json as _j
    import httpx
    convo = "\n".join(
        f"{'CUSTOMER' if m.get('role') == 'customer' else 'REP'}: {m.get('text','')}"
        for m in messages[-20:]
    )
    prompt = (
        "You are a live sales coach sitting beside a Rain Networks rep who is chatting with an IT "
        "reseller (the CUSTOMER) about selling Guardz to the reseller's own clients. Your job is to "
        "make the REP better. NOT to talk for them.\n\n"
        f"{CPP_STYLE_LIGHT}\n\n"
        "Return ONLY this JSON, no prose:\n"
        '{"health":"green|yellow|red","intent":<integer 0-100>,'
        '"tone":"<3-7 word read of the customer\'s mood/intent>",'
        '"tips":["<tip>","<tip>"]}\n\n'
        "TIPS RULES:\n"
        "- 1-3 tips, each a TERSE fragment, MAX 6 WORDS. No sentences, no reasoning. "
        "Good: 'Ask what prompted this', 'Find out what they sell', 'Mention free tier', "
        "'Acknowledge the price worry', 'Get their email'.\n"
        "- MATCH THE STAGE. Never jump ahead:\n"
        "  * Opening (first 1-2 exchanges): basic discovery ONLY. What prompted this, what they "
        "sell, who their clients are. NO pilots, pricing, or email yet.\n"
        "  * Middle (engaged or raising a concern): address the concern, tie to their clients' pain, "
        "mention the free tier or 'no security expertise needed'.\n"
        "  * Late (clearly interested, asking price or next steps): only now move to pricing, a pilot "
        "client, or getting their email.\n"
        "- Flag what they missed FOR THE CURRENT STAGE. If they did well, one tip can affirm "
        "(e.g. 'Good discovery question'). Never write their reply for them.\n\n"
        "health: green = engaged/buying; yellow = neutral/hesitant; red = objection/frustrated. "
        "Base everything ONLY on what was actually said.\n\n"
        f"CHAT:\n{convo}"
    )
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "HTTP-Referer": "https://interactivechat.up.railway.app",
                         "X-Title": "Rain Networks Copilot"},
                json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 300, "temperature": 0.3},
            )
        if resp.status_code == 200:
            txt = resp.json()["choices"][0]["message"]["content"]
            i, j = txt.find("{"), txt.rfind("}")
            if i != -1 and j != -1:
                return _j.loads(txt[i:j + 1])
        else:
            print(f"[copilot] OpenRouter {resp.status_code}: {resp.text[:160]}")
    except Exception as e:
        print(f"[copilot] error: {e}")
    return {}


async def _post_copilot_slack(coach: dict, messages: list):
    if not SLACK_WEBHOOK_URL or not coach:
        return
    emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(coach.get("health", "yellow"), "🟡")
    last_cust = next((m["text"] for m in reversed(messages) if m.get("role") == "customer"), "")
    tips = coach.get("tips") or ([coach["suggestion"]] if coach.get("suggestion") else [])
    tips_txt = "\n".join(f"- {t}" for t in tips if t) or "-  --"
    text = (f"{emoji} *Call health: {coach.get('health','?')}*  ·  Intent {coach.get('intent','?')}%\n"
            f"*Read:* {coach.get('tone','')}\n"
            f"*Customer:* {last_cust[:160]}\n"
            f"*Coaching:*\n{tips_txt}")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(SLACK_WEBHOOK_URL, json={"text": text})
    except Exception as e:
        print(f"[copilot-slack] {e}")


async def _run_copilot(session_id: str):
    chat = _chats.get(session_id)
    if not chat or not chat.get("messages"):
        return
    coach = await _generate_copilot(chat["messages"])
    if coach:
        chat["coach"] = coach
        await _post_copilot_slack(coach, chat["messages"])


async def _generate_customer_reply(messages: list) -> str:
    """AI plays the CUSTOMER (a reseller) and responds to the rep's actual words.
    NOTE: This is NOT Steve's voice. It simulates a different person (the prospect).
    CPP is intentionally NOT applied here."""
    if not OPENROUTER_API_KEY:
        return ""
    import httpx
    convo = "\n".join(
        f"{'REP' if m.get('role') == 'rep' else 'YOU'}: {m.get('text','')}"
        for m in messages[-20:]
    )
    prompt = (
        "You are role-playing the CUSTOMER in a live sales chat: an IT reseller / MSP owner exploring "
        "whether to resell Guardz to your own SMB clients (dental offices, law firms, accountants). "
        "Persona: busy, a little skeptical, price-sensitive, not a security expert, but genuinely "
        "interested if it makes you money without being a hassle. Respond naturally to the REP's last "
        "message in 1-3 short sentences, in character. Raise realistic concerns or objections when they "
        "fit, and warm up as the rep addresses them. If there is no conversation yet, open with a "
        "realistic first question about Guardz. Output ONLY your next line as the customer. No labels, "
        "no quotes.\n\n"
        f"CONVERSATION SO FAR:\n{convo if convo else '(none yet -- you start)'}"
    )
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "HTTP-Referer": "https://interactivechat.up.railway.app",
                         "X-Title": "Rain Networks Copilot"},
                json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 150, "temperature": 0.8},
            )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip().strip('"')
        print(f"[customer-sim] OpenRouter {resp.status_code}: {resp.text[:160]}")
    except Exception as e:
        print(f"[customer-sim] error: {e}")
    return ""


async def _customer_then_coach(sid: str):
    chat = _chats.setdefault(sid, {"messages": [], "coach": {}})
    reply = await _generate_customer_reply(chat["messages"])
    if reply:
        chat["messages"].append({"role": "customer", "text": reply,
                                 "ts": datetime.utcnow().isoformat()})
        if len(chat["messages"]) > 100:
            chat["messages"] = chat["messages"][-100:]
    await _run_copilot(sid)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CPP-PATCHED: _generate_rep_reply — uses full CPP_VOICE                   ║
# ║  This IS Steve talking. Full voice profile applied.                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

async def _generate_rep_reply(messages: list) -> str:
    """Draft a strong rep reply for the human to EDIT before sending ('Respond for me')."""
    if not OPENROUTER_API_KEY or not messages:
        return ""
    import httpx
    convo = "\n".join(
        f"{'YOU (rep)' if m.get('role') == 'rep' else 'CUSTOMER'}: {m.get('text','')}"
        for m in messages[-20:]
    )
    prompt = (
        "You are Steve Winfield, a top Rain Networks rep in a live chat with an IT reseller (the CUSTOMER) about "
        "reselling Guardz to their SMB clients.\n\n"
        f"{CPP_VOICE}\n\n"
        "Write your next reply to the customer's last message. "
        "Natural and concise (1-3 sentences), in Steve's real voice, moving the deal forward. Where it "
        "fits: tie Guardz to the clients' real pain, mention the free Community tier / $5-15 per user, "
        "offer the risk report they can show clients, reassure they don't need to be a security expert, "
        "ask which client to start with, and work toward their email + a callback. "
        "Output ONLY the reply text. No labels, no quotes.\n\n"
        f"CONVERSATION:\n{convo}"
    )
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "HTTP-Referer": "https://interactivechat.up.railway.app",
                         "X-Title": "Rain Networks Copilot"},
                json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 180, "temperature": 0.6},
            )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip().strip('"')
        print(f"[rep-suggest] OpenRouter {resp.status_code}: {resp.text[:160]}")
    except Exception as e:
        print(f"[rep-suggest] error: {e}")
    return ""


@app.post("/chat/send")
async def chat_send(msg: ChatMessage, background_tasks: BackgroundTasks):
    sid = msg.session_id or "copilot"
    chat = _chats.setdefault(sid, {"messages": [], "coach": {}})
    text = (msg.message or "").strip()
    if not text:
        return {"ok": False}
    role = "rep" if msg.role == "rep" else "customer"
    chat["messages"].append({"role": role, "text": text, "ts": datetime.utcnow().isoformat()})
    if len(chat["messages"]) > 100:
        chat["messages"] = chat["messages"][-100:]
    if role == "rep" and msg.simulate:
        background_tasks.add_task(_customer_then_coach, sid)
        return {"ok": True, "count": len(chat["messages"])}

    if role == "rep":
        chat["human_active"] = True
        background_tasks.add_task(_run_copilot, sid)
        return {"ok": True, "count": len(chat["messages"])}

    background_tasks.add_task(_run_copilot, sid)
    if not chat.get("slack_pinged"):
        chat["slack_pinged"] = True
        chat["started_at"] = datetime.utcnow().isoformat()
        wait = msg.wait if (msg.wait and msg.wait > 0) else GUARDZ_HUMAN_WAIT_SECONDS
        background_tasks.add_task(_ping_team_slack, sid, text, wait)
        asyncio.create_task(_ai_fallback_after(sid, wait))
    if chat.get("ai_active"):
        background_tasks.add_task(_guardz_then_capture, sid)
    return {"ok": True, "count": len(chat["messages"])}


async def _guardz_then_capture(sid: str):
    chat = _chats.get(sid)
    if not chat:
        return
    reply = await _generate_guardz_reply(chat["messages"])
    if reply:
        chat["messages"].append({"role": "agent", "text": reply, "ts": datetime.utcnow().isoformat()})
    await _guardz_capture(sid)


async def _ping_team_slack(sid: str, first_msg: str, wait: int):
    link = f"{PUBLIC_BASE_URL}/agent?session={sid}"
    if not SLACK_WEBHOOK_URL:
        print(f"[team-ping] (no SLACK_WEBHOOK_URL) new chat {sid}: {first_msg[:80]} | claim: {link}")
        return
    text = (f"🟢 *New Guardz chat* -- a visitor wants to talk.\n> {first_msg[:200]}\n"
            f"*Claim it (first to reply takes it):* {link}\n"
            f"_AI takes over in ~{wait}s if no one grabs it._")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(SLACK_WEBHOOK_URL, json={"text": text})
    except Exception as e:
        print(f"[team-ping] {e}")


async def _ai_fallback_after(sid: str, wait: int):
    try:
        await asyncio.sleep(max(1, wait))
        chat = _chats.get(sid)
        if not chat or chat.get("human_active") or chat.get("ai_active"):
            return
        chat["ai_active"] = True
        reply = await _generate_guardz_reply(chat["messages"])
        intro = ("Thanks for your patience! Our specialists are all tied up right now, so I'll jump in "
                 "directly. I'm the Guardz AI assistant. ")
        chat["messages"].append({"role": "agent",
                                 "text": intro + (reply or "What can I tell you about Guardz?"),
                                 "ts": datetime.utcnow().isoformat()})
        await _guardz_capture(sid)
    except Exception as e:
        print(f"[ai-fallback] {e}")


@app.get("/chat/state")
async def chat_state(session_id: str = "copilot"):
    chat = _chats.get(session_id, {"messages": [], "coach": {}})
    if chat.get("human_active"):
        mode = "human"
    elif chat.get("ai_active"):
        mode = "ai"
    elif chat.get("slack_pinged"):
        mode = "waiting"
    else:
        mode = "idle"
    return {"messages": chat.get("messages", []), "coach": chat.get("coach", {}), "mode": mode}


@app.get("/chat/reset")
async def chat_reset(session_id: str = "copilot"):
    _chats.pop(session_id, None)
    return {"ok": True}


@app.post("/chat/customer-turn")
async def chat_customer_turn(background_tasks: BackgroundTasks, session_id: str = "copilot"):
    background_tasks.add_task(_customer_then_coach, session_id)
    return {"ok": True}


@app.post("/chat/suggest-rep")
async def chat_suggest_rep(session_id: str = "copilot"):
    chat = _chats.get(session_id, {"messages": []})
    return {"suggestion": await _generate_rep_reply(chat.get("messages", []))}


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CPP-PATCHED: _generate_guardz_reply — uses full CPP_VOICE                ║
# ║  This represents Steve / Rain Networks to visitors. Full voice applied.    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class GuardzChatRequest(BaseModel):
    session_id: str = ""
    message: str = ""


async def _generate_guardz_reply(messages: list) -> str:
    """Friendly Guardz expert answering a website visitor on the Guardz page."""
    if not OPENROUTER_API_KEY:
        return "Thanks for stopping by! The assistant is warming up... try again in a moment."
    import httpx
    convo = "\n".join(
        f"{'VISITOR' if m.get('role') == 'customer' else 'YOU'}: {m.get('text','')}"
        for m in messages[-20:]
    )
    prompt = (
        "You are Steve Winfield, a friendly, sharp Guardz expert at Rain Networks, chatting with a visitor on the "
        "Guardz product page. The visitor is usually an IT reseller / MSP weighing whether to offer "
        "Guardz to their SMB clients.\n\n"
        f"{CPP_VOICE}\n\n"
        "Answer clearly and concisely. 1-3 short, conversational "
        "sentences, never an info-dump. Be helpful first. Naturally learn what they sell and who their "
        "clients are so you can make it relevant. When the moment feels right, offer to have a "
        "specialist follow up and ask for their email. Don't force it early.\n\n"
        "GUARDZ FACTS: all-in-one cybersecurity + cyber insurance platform for MSPs/resellers serving "
        "SMBs. Email security, EDR (SentinelOne), identity threat detection, cloud security "
        "(M365/Google), security awareness training, phishing simulation, and external footprint "
        "scanning, in one multi-tenant console. Free Community tier; Pro and Ultimate per-user/mo "
        "(Ultimate includes SentinelOne MDR); no enterprise commitment. 2025 MSP Today Product of the "
        "Year; $56M Series B. Partners typically add it at $5-15/user on existing contracts.\n\n"
        "If asked, you're Steve, the Guardz AI assistant for Rain Networks (don't claim to be human). "
        "Output ONLY your next reply.\n\n"
        f"CONVERSATION:\n{convo if convo else '(the visitor just opened the chat -- greet them warmly and ask what brought them in)'}"
    )
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "HTTP-Referer": "https://interactivechat.up.railway.app",
                         "X-Title": "Rain Networks Guardz"},
                json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 220, "temperature": 0.5},
            )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip().strip('"')
        print(f"[guardz-chat] OpenRouter {resp.status_code}: {resp.text[:160]}")
    except Exception as e:
        print(f"[guardz-chat] error: {e}")
    return "Sorry... hit a snag. Mind trying that again?"


async def _guardz_capture(sid: str):
    chat = _chats.get(sid)
    if not chat:
        return
    msgs = chat.get("messages", [])
    email = _extract_email(_transcript_text(msgs))
    if not email:
        return
    st = _handoff_state.setdefault(sid, {})
    if st.get("notified"):
        return
    st["notified"] = True
    await _save_lead(session_id=sid, name="", company="", email=email, phone="",
                     vertical="", signal="web_guardz_chat", handed_off=False,
                     brief="", transcript=msgs)
    import html as _h
    lines = "".join(
        f"<div style='margin:4px 0'><b>{'Visitor' if m.get('role')=='customer' else 'Guardz AI'}:</b> "
        f"{_h.escape(m.get('text',''))}</div>" for m in msgs[-14:]
    )
    body = (f"<div style=\"font-family:-apple-system,Segoe UI,sans-serif;color:#1f2937\">"
            f"<h3>New Guardz-page chat lead</h3><p><b>Email:</b> {_h.escape(email)}</p><hr>{lines}</div>")
    ok, detail = await send_email(SALES_TEAM_EMAIL, f"🌐 Guardz page lead -- {email}", body)
    print(f"[guardz-chat] lead {email} -> {SALES_TEAM_EMAIL}: ok={ok} ({detail})")


@app.post("/guardz/chat")
async def guardz_chat(req: GuardzChatRequest, background_tasks: BackgroundTasks):
    sid = "guardz:" + ((req.session_id or "web").strip() or "web")
    chat = _chats.setdefault(sid, {"messages": [], "coach": {}})
    msg = (req.message or "").strip()
    if msg:
        chat["messages"].append({"role": "customer", "text": msg, "ts": datetime.utcnow().isoformat()})
        if len(chat["messages"]) > 100:
            chat["messages"] = chat["messages"][-100:]
    if chat.get("human_active"):
        background_tasks.add_task(_guardz_capture, sid)
        return {"reply": "", "pending_human": True}
    reply = await _generate_guardz_reply(chat["messages"])
    if reply:
        chat["messages"].append({"role": "agent", "text": reply, "ts": datetime.utcnow().isoformat()})
    background_tasks.add_task(_guardz_capture, sid)
    return {"reply": reply, "pending_human": False}

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


# ─── HTML Pages (unchanged — see original for the full demo/live page) ────────
# NOTE: The HTML pages are identical to the original. They are omitted here for
# brevity. Copy them from the original api.py (the root /, /copilot, /agent,
# /visitor, /demo/live endpoints with their HTML string literals).
# The VOICE_SDK_JS and all HTML pages are unchanged.
# ──────────────────────────────────────────────────────────────────────────────

# ─── PLACEHOLDER: Copy the HTML page endpoints from the original api.py ───────
# The following endpoints need the original HTML strings copied in:
#   @app.get("/")              -> root()
#   @app.get("/copilot")       -> copilot_page()
#   @app.get("/agent")         -> agent_console()
#   @app.get("/visitor")       -> visitor_page()
#   @app.get("/demo/live")     -> demo_live()
#
# These are pure HTML/JS strings with no LLM calls, so no CPP changes needed.
# They were omitted here only to keep this patch file focused on the 5 LLM changes.
