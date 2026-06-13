"""
api.py
------
FastAPI layer for the research agent.
Exposes endpoints: POST /research/company, GET /research/status/{session_id},
POST /notify/human, GET /health

Run locally:
    uvicorn api:app --reload

Deploy on Railway:
    Set env vars: OPENROUTER_API_KEY, TAVILY_API_KEY, DATABASE_URL, SLACK_WEBHOOK_URL
"""

import asyncio
import os
from datetime import datetime

import asyncpg
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from company_research import ResearchResult, domain_from_email, research_company

app = FastAPI(title="Research Agent", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv("DATABASE_URL", "")


# ─────────────────────────────────────────
# DB pool
# ─────────────────────────────────────────

_pool: asyncpg.Pool | None = None


def _fix_db_url(url: str) -> str:
    """Railway gives postgresql://, asyncpg needs postgres://"""
    return url.replace("postgresql://", "postgres://", 1)


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(_fix_db_url(DATABASE_URL), min_size=2, max_size=10)
    return _pool


@app.on_event("startup")
async def startup():
    # Don't crash on startup if DB isn't ready — pool connects lazily
    if DATABASE_URL:
        try:
            await get_pool()
        except Exception as e:
            print(f"[startup] DB connection failed, will retry on first request: {e}")


@app.on_event("shutdown")
async def shutdown():
    if _pool:
        await _pool.close()


# ─────────────────────────────────────────
# Request / Response models
# ─────────────────────────────────────────

class ResearchRequest(BaseModel):
    domain: str | None = None
    email: str | None = None
    session_id: str = ""
    force_refresh: bool = False


class ResearchStatusResponse(BaseModel):
    session_id: str
    domain: str
    status: str                     # pending | running | done | failed
    result: dict | None = None
    error: str | None = None


# ─────────────────────────────────────────
# Cache helpers
# ─────────────────────────────────────────

async def _get_cached_company(pool: asyncpg.Pool, domain: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT * FROM company_profiles WHERE domain = $1 AND expires_at > NOW()",
        domain,
    )
    return dict(row) if row else None


async def _get_cached_industry(pool: asyncpg.Pool, industry: str, sub_industry: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT * FROM industry_profiles WHERE industry = $1 AND sub_industry = $2 AND expires_at > NOW()",
        industry, sub_industry,
    )
    return dict(row) if row else None


async def _save_company(pool: asyncpg.Pool, c) -> int:
    import json as _json
    row = await pool.fetchrow(
        """INSERT INTO company_profiles
           (domain, company_name, description, industry, sub_industry, company_size,
            estimated_revenue, founded_year, hq_location, business_model,
            tech_stack_signals, recent_news, funding_stage, key_competitors,
            linkedin_url, confidence, research_notes, researched_at, expires_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,NOW(),NOW() + INTERVAL '30 days')
           ON CONFLICT (domain) DO UPDATE SET
               company_name=EXCLUDED.company_name, description=EXCLUDED.description,
               industry=EXCLUDED.industry, sub_industry=EXCLUDED.sub_industry,
               company_size=EXCLUDED.company_size, estimated_revenue=EXCLUDED.estimated_revenue,
               founded_year=EXCLUDED.founded_year, hq_location=EXCLUDED.hq_location,
               business_model=EXCLUDED.business_model, tech_stack_signals=EXCLUDED.tech_stack_signals,
               recent_news=EXCLUDED.recent_news, funding_stage=EXCLUDED.funding_stage,
               key_competitors=EXCLUDED.key_competitors, linkedin_url=EXCLUDED.linkedin_url,
               confidence=EXCLUDED.confidence, research_notes=EXCLUDED.research_notes,
               researched_at=NOW(), expires_at=NOW() + INTERVAL '30 days'
           RETURNING id""",
        c.domain, c.company_name, c.description, c.industry, c.sub_industry,
        c.company_size, c.estimated_revenue, c.founded_year, c.hq_location,
        c.business_model,
        _json.dumps(c.tech_stack_signals), _json.dumps(c.recent_news),
        c.funding_stage, _json.dumps(c.key_competitors),
        c.linkedin_url, c.confidence, c.research_notes,
    )
    return row["id"]


async def _save_industry(pool: asyncpg.Pool, i) -> int:
    import json as _json
    row = await pool.fetchrow(
        """INSERT INTO industry_profiles
           (industry, sub_industry, top_pain_points, buying_triggers, common_objections,
            key_metrics, industry_trends, regulatory_pressures,
            typical_decision_makers, average_sales_cycle, researched_at, expires_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW(),NOW() + INTERVAL '14 days')
           ON CONFLICT (industry, sub_industry) DO UPDATE SET
               top_pain_points=EXCLUDED.top_pain_points, buying_triggers=EXCLUDED.buying_triggers,
               common_objections=EXCLUDED.common_objections, key_metrics=EXCLUDED.key_metrics,
               industry_trends=EXCLUDED.industry_trends, regulatory_pressures=EXCLUDED.regulatory_pressures,
               typical_decision_makers=EXCLUDED.typical_decision_makers,
               average_sales_cycle=EXCLUDED.average_sales_cycle,
               researched_at=NOW(), expires_at=NOW() + INTERVAL '14 days'
           RETURNING id""",
        i.industry, i.sub_industry,
        _json.dumps(i.top_pain_points), _json.dumps(i.buying_triggers),
        _json.dumps(i.common_objections), _json.dumps(i.key_metrics_they_care_about),
        _json.dumps(i.industry_trends), _json.dumps(i.regulatory_pressures),
        _json.dumps(i.typical_decision_makers), i.average_sales_cycle,
    )
    return row["id"]


# ─────────────────────────────────────────
# Background research task
# ─────────────────────────────────────────

async def _run_research_job(job_id: int, domain: str, session_id: str):
    pool = await get_pool()
    await pool.execute(
        "UPDATE research_jobs SET status='running', started_at=NOW() WHERE id=$1", job_id,
    )
    try:
        result: ResearchResult = await research_company(domain)
        company_id = await _save_company(pool, result.company)
        industry_id = await _save_industry(pool, result.industry)
        await pool.execute(
            """UPDATE research_jobs SET status='done', completed_at=NOW(),
               duration_seconds=$1, company_profile_id=$2, industry_profile_id=$3
               WHERE id=$4""",
            result.duration_seconds, company_id, industry_id, job_id,
        )
    except Exception as e:
        await pool.execute(
            "UPDATE research_jobs SET status='failed', error_message=$1, completed_at=NOW() WHERE id=$2",
            str(e), job_id,
        )
        raise


# ─────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────

@app.post("/research/company", response_model=ResearchStatusResponse)
async def start_research(req: ResearchRequest, background_tasks: BackgroundTasks):
    domain = req.domain or ""
    if not domain and req.email:
        domain = domain_from_email(req.email)
    if not domain:
        raise HTTPException(status_code=400, detail="Provide a 'domain' or business 'email'.")

    pool = await get_pool()

    if not req.force_refresh:
        cached = await _get_cached_company(pool, domain)
        if cached:
            ind = await _get_cached_industry(pool, cached["industry"] or "", cached["sub_industry"] or "")
            return ResearchStatusResponse(
                session_id=req.session_id, domain=domain, status="done",
                result={"company": dict(cached), "industry": dict(ind) if ind else {}, "source": "cache"},
            )

    job_id = await pool.fetchval(
        "INSERT INTO research_jobs (session_id, domain, status) VALUES ($1, $2, 'pending') RETURNING id",
        req.session_id, domain,
    )
    background_tasks.add_task(_run_research_job, job_id, domain, req.session_id)
    return ResearchStatusResponse(session_id=req.session_id, domain=domain, status="pending")


@app.get("/research/status/{session_id}", response_model=ResearchStatusResponse)
async def get_research_status(session_id: str):
    pool = await get_pool()
    job = await pool.fetchrow(
        """SELECT j.*, c.*, ip.*
           FROM research_jobs j
           LEFT JOIN company_profiles c ON c.id = j.company_profile_id
           LEFT JOIN industry_profiles ip ON ip.id = j.industry_profile_id
           WHERE j.session_id = $1 ORDER BY j.id DESC LIMIT 1""",
        session_id,
    )
    if not job:
        raise HTTPException(status_code=404, detail="No research job found for this session")

    job = dict(job)
    status = job["status"]

    if status == "done":
        return ResearchStatusResponse(
            session_id=session_id, domain=job["domain"], status="done",
            result={
                "company": {k: job[k] for k in [
                    "domain","company_name","description","industry","sub_industry",
                    "company_size","estimated_revenue","founded_year","hq_location",
                    "business_model","tech_stack_signals","recent_news","funding_stage",
                    "key_competitors","linkedin_url","confidence","research_notes",
                ] if k in job},
                "industry": {k: job[k] for k in [
                    "top_pain_points","buying_triggers","common_objections","key_metrics",
                    "industry_trends","regulatory_pressures","typical_decision_makers","average_sales_cycle",
                ] if k in job},
                "duration_seconds": job.get("duration_seconds"),
            },
        )
    if status == "failed":
        return ResearchStatusResponse(
            session_id=session_id, domain=job["domain"], status="failed", error=job.get("error_message"),
        )
    return ResearchStatusResponse(session_id=session_id, domain=job["domain"], status=status)


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ─────────────────────────────────────────
# Human alert (Slack)
# ─────────────────────────────────────────

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
    if not SLACK_WEBHOOK_URL:
        return NotifyHumanResponse(ok=True, detail="SLACK_WEBHOOK_URL not configured")

    signal_label = SIGNAL_LABELS.get(req.signal_type, SIGNAL_LABELS["other"])
    convo_url = (
        f"https://elevenlabs.io/app/conversational-ai/conversations/{req.session_id}"
        if req.session_id else ""
    )

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🔥 HOT LEAD — Transfer Ready", "emoji": True}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Name:*\n{req.prospect_name or '(not collected)'}"},
            {"type": "mrkdwn", "text": f"*Company:*\n{req.company_name or '(unknown)'}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Signal:* {signal_label}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*They said:*\n> {req.message or '(no message captured)'}"}},
    ]
    if convo_url:
        blocks.append({"type": "actions", "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": "View Live Conversation", "emoji": True},
            "url": convo_url, "style": "primary",
        }]})

    payload = {
        "text": f"🔥 HOT LEAD: {req.prospect_name or 'Unknown'} @ {req.company_name or 'Unknown'} — {signal_label}",
        "blocks": blocks,
    }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(SLACK_WEBHOOK_URL, json=payload)
            resp.raise_for_status()
        return NotifyHumanResponse(ok=True)
    except Exception as e:
        return NotifyHumanResponse(ok=False, detail=str(e))
