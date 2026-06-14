"""
api.py — Guardz Research Agent + Live Demo
"""

import os
from datetime import datetime

import asyncpg
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from company_research import ResearchResult, domain_from_email, research_company

app = FastAPI(title="Guardz Research Agent", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATABASE_URL = os.getenv("DATABASE_URL", "")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "agent_5101kty2ztmme25aspqycwp7mpsm")

# ─── Demo state (in-memory, resets on redeploy) ───────────────────────────────

_demo_jobs: list[dict] = []   # most-recent first, max 10
_demo_signals: list[dict] = []

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

class NotifyHumanResponse(BaseModel):
    ok: bool
    detail: str = ""

@app.post("/notify/human", response_model=NotifyHumanResponse)
async def notify_human(req: NotifyHumanRequest):
    label = SIGNAL_LABELS.get(req.signal_type, SIGNAL_LABELS["other"])

    # Always track in demo state
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

    if not SLACK_WEBHOOK_URL:
        return NotifyHumanResponse(ok=True, detail="SLACK_WEBHOOK_URL not configured")

    convo_url = (f"https://elevenlabs.io/app/conversational-ai/conversations/{req.session_id}"
                 if req.session_id else "")
    blocks = [
        {"type":"header","text":{"type":"plain_text","text":"🔥 HOT LEAD — Transfer Ready","emoji":True}},
        {"type":"section","fields":[
            {"type":"mrkdwn","text":f"*Name:*\n{req.prospect_name or '(not collected)'}"},
            {"type":"mrkdwn","text":f"*Company:*\n{req.company_name or '(unknown)'}"},
        ]},
        {"type":"section","text":{"type":"mrkdwn","text":f"*Signal:* {label}"}},
        {"type":"section","text":{"type":"mrkdwn","text":f"*They said:*\n> {req.message or '(no message captured)'}"}},
    ]
    if convo_url:
        blocks.append({"type":"actions","elements":[{
            "type":"button","style":"primary","url":convo_url,
            "text":{"type":"plain_text","text":"View Live Conversation","emoji":True}}]})
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(SLACK_WEBHOOK_URL,
                json={"text":f"🔥 {req.prospect_name or 'Unknown'} @ {req.company_name or 'Unknown'} — {label}","blocks":blocks})
            resp.raise_for_status()
        return NotifyHumanResponse(ok=True)
    except Exception as e:
        return NotifyHumanResponse(ok=False, detail=str(e))


# ─── Demo state API ───────────────────────────────────────────────────────────

@app.get("/demo/state")
async def demo_state():
    return {"jobs": _demo_jobs, "signals": _demo_signals}

@app.get("/demo/reset")
async def demo_reset():
    _demo_jobs.clear()
    _demo_signals.clear()
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
    { name:"Mike Torres", company:"TechForce IT Solutions", city:"Tampa, FL", email:"mike@techforceit.com",
      selling:"Dental offices — 8 clients asking about cyber insurance compliance",
      questions:["Does Guardz satisfy what cyber insurers are now requiring — EDR, email security, all of it?","Can I show a prospect their risk score before they sign with me?"] },
    { name:"Sarah Kim", company:"BlueSky Systems", city:"Phoenix, AZ", email:"sarah@blueskysystems.com",
      selling:"Law firms — worried about client data breach liability",
      questions:["Does Guardz cover identity threats and email together in one platform?","What does onboarding look like for a 15-seat law firm?"] },
    { name:"Dave Okonkwo", company:"PrecisionTech IT", city:"Houston, TX", email:"dave@precisiontechit.com",
      selling:"CPA firms — clients asking about financial data protection",
      questions:["My clients are mostly on Microsoft 365 — does Guardz handle M365 email security?","What's the margin like for a partner my size?"] },
    { name:"Jennifer Walsh", company:"Walsh IT Services", city:"Chicago, IL", email:"jen@walshit.com",
      selling:"Real estate agencies — handling sensitive client financial data",
      questions:["Does Guardz include security awareness training and phishing simulation?","I'm not a security expert — how much do I need to know to sell this?"] },
    { name:"Carlos Rivera", company:"RiverTech Solutions", city:"Miami, FL", email:"carlos@rivertechfl.com",
      selling:"Small manufacturers — hit by ransomware last year, now paranoid",
      questions:["What does the MDR piece actually mean — who's watching alerts at 2am?","Can Guardz prevent ransomware, or just detect it after the fact?"] },
    { name:"Amanda Chu", company:"Summit IT Group", city:"Seattle, WA", email:"amanda@summitit.com",
      selling:"Healthcare clinics — HIPAA compliance pressure increasing",
      questions:["Does Guardz help clients meet HIPAA security requirements?","How does the cyber insurance piece work — do you broker it or just help qualify?"] },
    { name:"Brian Murphy", company:"Murphy Networks", city:"Boston, MA", email:"brian@murphynetworks.com",
      selling:"Financial services firms — regulators asking hard questions",
      questions:["I'm losing deals to bigger MSPs with managed security. Can Guardz close that gap?","What's the partner program structure — tiers, margins, support?"] },
    { name:"Lisa Patel", company:"Patel IT Consulting", city:"Dallas, TX", email:"lisa@patelitconsulting.com",
      selling:"Construction companies — new to security, don't know where to start",
      questions:["Can I manage all my clients from one dashboard?","How fast can I actually get a client up and running?"] },
    { name:"Tom Nguyen", company:"NextWave IT", city:"Denver, CO", email:"tom@nextwaveit.com",
      selling:"Accounting firms — clients storing sensitive tax data",
      questions:["One of my clients just got a ransomware demand — could Guardz have prevented that?","Does Guardz work alongside existing tools or does it replace everything?"] },
    { name:"Rachel Stevens", company:"Stevens Technology", city:"Atlanta, GA", email:"rachel@stevenstech.com",
      selling:"Medical practices — patients asking about data security after hospital breaches in the news",
      questions:["Does Guardz cover identity threat detection — like compromised employee accounts?","What's the pricing — per user, per client, flat rate?"] },
    { name:"Kevin Park", company:"KP Systems", city:"San Jose, CA", email:"kevin@kpsystems.io",
      selling:"Startups and small tech companies — SOC 2 compliance coming up",
      questions:["Can Guardz help clients build the evidence they need for SOC 2 or cyber insurance audits?","Is there a free tier I can use to show a client before committing?"] },
    { name:"Greg Henderson", company:"Delta IT Services", city:"Nashville, TN", email:"greg@deltait.com",
      selling:"Insurance agencies — ironic that they have no security themselves",
      questions:["How does the external footprint scanning work — does it find things clients don't know are exposed?","What kind of reporting can I give a client after a scan?"] },
    { name:"Nikki Brown", company:"Brown Tech Consulting", city:"Portland, OR", email:"nikki@browntech.com",
      selling:"Architecture and engineering firms — lots of intellectual property to protect",
      questions:["Does Guardz protect against insider threats or just external attacks?","I currently use a patchwork of tools — what does Guardz actually replace?"] },
    { name:"James Wilson", company:"Wilson IT Partners", city:"Charlotte, NC", email:"james@wilsonit.com",
      selling:"Dental group practices — multiple locations, one IT partner",
      questions:["Can I run one Guardz deployment across multiple client locations?","My clients keep asking me about cyber insurance — can Guardz help them qualify?"] },
    { name:"Maria Santos", company:"Santos IT", city:"Orlando, FL", email:"maria@santosit.com",
      selling:"Retail businesses — POS systems and customer payment data",
      questions:["Does Guardz cover endpoint security or is it just monitoring?","I have clients who think they're too small to be targeted — how do I make the case?"] },
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

  <!-- Panel 3: Live Intel -->
  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">Panel 3</div>
      <div class="panel-status">🔔 Live Sales Intel</div>
    </div>
    <div class="panel-body" id="signals-body">
      <div class="no-signals">
        <div class="no-signals-icon">🔔</div>
        <div class="no-signals-text">Signals appear here when the prospect<br>shows buying intent or requests a demo</div>
      </div>
    </div>
  </div>

</div>

<!-- Guardz Sales Agent Widget -->
<elevenlabs-convai agent-id="{ELEVENLABS_AGENT_ID}"></elevenlabs-convai>
<script src="https://elevenlabs.io/convai-widget/index.js" async type="text/javascript"></script>

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

function renderSignals(signals) {{
  const body = document.getElementById('signals-body');
  if (!signals || signals.length === 0) return;

  const isTransfer = s => ['demo_agreed','handoff_requested','strong_interest'].includes(s.signal_type);

  body.innerHTML = signals.map(s => `
    <div class="${{isTransfer(s) ? 'transfer-alert' : 'signal-card'}}">
      <div class="signal-label">${{s.label || s.signal_type}}</div>
      <div class="signal-who">
        ${{s.prospect_name || 'Prospect'}}${{s.company_name ? ' · ' + s.company_name : ''}}
      </div>
      ${{s.message ? `<div class="signal-quote">"${{s.message}}"</div>` : ''}}
      ${{isTransfer(s) ? `<div style="font-size:12px;color:#10b981;margin-top:8px;font-weight:600">📞 Transferring to +14257538897</div>` : ''}}
      <div class="signal-time">${{fmtTime(s.timestamp)}}</div>
    </div>`).join('');
}}

async function poll() {{
  try {{
    const r = await fetch('/demo/state');
    const data = await r.json();
    if (data.jobs && data.jobs.length > 0) {{
      renderResearch(data.jobs[0]);
    }}
    if (data.signals && data.signals.length !== lastSignalCount) {{
      lastSignalCount = data.signals.length;
      renderSignals(data.signals);
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
  document.getElementById('signals-body').innerHTML = `
    <div class="no-signals">
      <div class="no-signals-icon">🔔</div>
      <div class="no-signals-text">Signals appear here when the prospect<br>shows buying intent or requests a demo</div>
    </div>`;
  document.getElementById('chat-status').textContent = 'Waiting for conversation to start…';
}}

setInterval(poll, 2000);
</script>
</body>
</html>""")
