"""
company_research.py
-------------------
Research pipeline: domain → CompanyProfile + IndustryProfile + SalesContext

PRIMARY:  TDE (Targeted Decomposition Engine) — cached, multi-agent research swarm
FALLBACK: Tavily web search + OpenRouter synthesis (if TDE is unreachable)

Env vars:
  TDE_API_URL       - e.g. https://targeteddecomposition-production.up.railway.app
  TDE_API_KEY       - API_SECRET_KEY from TDE Railway service
  OPENROUTER_API_KEY, OPENROUTER_MODEL, TAVILY_API_KEY  (fallback)
"""

import asyncio
import json
import os
from datetime import datetime
from typing import Optional

import httpx
from openai import AsyncOpenAI
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from tavily import AsyncTavilyClient

load_dotenv()

TDE_URL = os.getenv("TDE_API_URL", "").rstrip("/")
TDE_KEY = os.getenv("TDE_API_KEY", "")

openrouter = AsyncOpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1",
)
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4-5")

_tavily_key = os.getenv("TAVILY_API_KEY", "")
tavily = AsyncTavilyClient(api_key=_tavily_key) if _tavily_key else None


# ──────────────────────────────────────────────────────────
# Pydantic models (unchanged — ElevenLabs prompt + demo page depend on these)
# ──────────────────────────────────────────────────────────

class CompanyProfile(BaseModel):
    domain: str
    company_name: str = ""
    description: str = ""
    industry: str = ""
    sub_industry: str = ""
    company_size: str = ""
    estimated_revenue: str = ""
    founded_year: Optional[int] = None
    hq_location: str = ""
    business_model: str = ""
    tech_stack_signals: list[str] = Field(default_factory=list)
    recent_news: list[str] = Field(default_factory=list)
    funding_stage: str = ""
    key_competitors: list[str] = Field(default_factory=list)
    linkedin_url: str = ""
    confidence: str = "low"
    research_notes: str = ""
    researched_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class IndustryProfile(BaseModel):
    industry: str
    sub_industry: str = ""
    top_pain_points: list[str] = Field(default_factory=list)
    buying_triggers: list[str] = Field(default_factory=list)
    common_objections: list[str] = Field(default_factory=list)
    key_metrics_they_care_about: list[str] = Field(default_factory=list)
    industry_trends: list[str] = Field(default_factory=list)
    regulatory_pressures: list[str] = Field(default_factory=list)
    typical_decision_makers: list[str] = Field(default_factory=list)
    average_sales_cycle: str = ""
    researched_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class SalesContext(BaseModel):
    domain: str
    confidence: str
    primary_source: str   # "company" | "blended" | "industry"
    company_name: str = ""
    company_description: str = ""
    company_size: str = ""
    company_revenue: str = ""
    company_funding: str = ""
    company_hq: str = ""
    company_business_model: str = ""
    company_tech_stack: list[str] = Field(default_factory=list)
    company_recent_news: list[str] = Field(default_factory=list)
    company_competitors: list[str] = Field(default_factory=list)
    industry: str
    sub_industry: str = ""
    pain_points: list[str] = Field(default_factory=list)
    buying_triggers: list[str] = Field(default_factory=list)
    common_objections: list[str] = Field(default_factory=list)
    key_metrics: list[str] = Field(default_factory=list)
    typical_decision_makers: list[str] = Field(default_factory=list)
    average_sales_cycle: str = ""
    industry_trends: list[str] = Field(default_factory=list)
    regulatory_pressures: list[str] = Field(default_factory=list)
    context_note: str = ""


class ResearchResult(BaseModel):
    domain: str
    company: CompanyProfile
    industry: IndustryProfile
    sales_context: Optional[SalesContext] = None
    duration_seconds: float = 0.0
    source: str = "tavily"   # "tde" | "tavily"


# ──────────────────────────────────────────────────────────
# TDE integration
#
# Response shapes:
#   POST /intel/research/company  → { source, domain, company_name,
#                                     intel:{company_name,website,industry,sub_industry,country} }
#     (minimal — rich data saved to DB but NOT returned here)
#   GET  /intel/company/:domain   → { found, domain, company_name, industry, sub_industry, country,
#                                     sections:{ painpoints:{data:{company_pain_points,technology_stack,...}},
#                                                compete:{data:{competitors,...}},
#                                                customer:{data:{recent_news,...}},
#                                                leadership:{data:[{name,title}]} } }
#   POST /intel/research/industry → { source, intel:{ industry_name,
#                                                     pain_points:[{title,description,urgency,persona}],
#                                                     trends:[{title,description,impact}],
#                                                     regulations:[{name,description,impact}] } }
# ──────────────────────────────────────────────────────────

def _tde_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if TDE_KEY:
        h["x-api-key"] = TDE_KEY
    return h


def _extract_strings(items, *keys) -> list:
    """Turn a list of strings or dicts into a flat list of strings."""
    out = []
    for item in (items or []):
        if isinstance(item, str) and item:
            out.append(item)
        elif isinstance(item, dict):
            for k in keys:
                v = item.get(k)
                if v and isinstance(v, str):
                    out.append(v)
                    break
    return out


def _map_tde_company_full(row: dict, domain: str) -> CompanyProfile:
    """
    Map GET /intel/company/:domain full response → CompanyProfile.
    Sections: painpoints, compete, customer, leadership.
    """
    sections = row.get("sections") or {}
    pp   = (sections.get("painpoints") or {}).get("data") or {}
    comp = (sections.get("compete") or {}).get("data") or {}
    cust = (sections.get("customer") or {}).get("data") or {}

    pain        = pp.get("company_pain_points") or pp.get("pain_points") or []
    tech        = pp.get("technology_stack") or cust.get("technology_stack") or []
    news        = cust.get("recent_news") or []
    competitors = comp.get("competitors") or []

    filled = sum(bool(row.get(f)) for f in [
        "company_name", "industry", "sub_industry", "country", "website",
    ])
    has_sections = bool(pp or comp or cust)
    confidence = "high" if (filled >= 4 and has_sections) else "medium" if filled >= 2 else "low"

    pain_strs = _extract_strings(pain, "title", "description")

    return CompanyProfile(
        domain=domain,
        company_name=row.get("company_name") or "",
        description=cust.get("description") or "",
        industry=row.get("industry") or "",
        sub_industry=row.get("sub_industry") or "",
        company_size="",
        estimated_revenue="",
        founded_year=None,
        hq_location=row.get("country") or row.get("address") or "",
        business_model=cust.get("pricing_model") or "Services",
        tech_stack_signals=_extract_strings(tech if isinstance(tech, list) else [tech]),
        recent_news=_extract_strings(
            news if isinstance(news, list) else [news], "title", "headline", "text"
        ),
        funding_stage="Unknown",
        key_competitors=_extract_strings(
            competitors if isinstance(competitors, list) else [competitors]
        ),
        linkedin_url=row.get("linkedin_url") or "",
        confidence=confidence,
        research_notes=(
            "Sourced from TDE swarm. Pain signals: "
            + (", ".join(pain_strs[:2]) if pain_strs else "none extracted")
        ),
    )


def _map_tde_industry(d: dict, industry: str, sub_industry: str) -> IndustryProfile:
    """
    Map POST /intel/research/industry response → IndustryProfile.
    pain_points / trends / regulations are lists of dicts with title/description.
    """
    intel = d.get("intel") or d

    pain_pts = _extract_strings(intel.get("pain_points") or [], "title", "description")
    trends   = _extract_strings(intel.get("trends") or [], "title", "description")
    regs     = _extract_strings(intel.get("regulations") or [], "name", "description")

    return IndustryProfile(
        industry=intel.get("industry_name") or intel.get("industry") or industry,
        sub_industry=intel.get("sub_industry") or sub_industry,
        top_pain_points=pain_pts,
        buying_triggers=[],
        common_objections=[],
        key_metrics_they_care_about=[],
        industry_trends=trends,
        regulatory_pressures=regs,
        typical_decision_makers=[],
        average_sales_cycle="",
    )


async def _tde_research_company(domain: str) -> Optional[CompanyProfile]:
    """
    2-step TDE company research:
      Step 1: POST /intel/research/company  — triggers swarm if cache miss, saves rich data to DB.
              Returns only minimal intel in the response body.
      Step 2: GET  /intel/company/:domain   — reads full rich data from DB (all sections).

    Timeout: 90s for step 1 (fresh research = 4 LLM agents + web fetch).
    """
    if not TDE_URL:
        print("[TDE] TDE_API_URL not set — skipping TDE")
        return None

    headers = _tde_headers()
    payload = {"url": f"https://{domain}", "role": "partner", "name": ""}

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            # Step 1: trigger research
            r1 = await client.post(
                f"{TDE_URL}/intel/research/company",
                json=payload, headers=headers,
            )
            if r1.status_code != 200:
                print(f"[TDE] POST research HTTP {r1.status_code} for {domain}: {r1.text[:300]}")
                return None
            src = r1.json().get("source", "?")
            print(f"[TDE] Step 1 done: source={src} for {domain}")

            # Step 2: fetch full data from DB
            r2 = await client.get(
                f"{TDE_URL}/intel/company/{domain}",
                headers=headers,
            )
            if r2.status_code != 200:
                print(f"[TDE] GET full data HTTP {r2.status_code} for {domain}: {r2.text[:200]}")
                return None
            full = r2.json()
            if not full.get("found"):
                print(f"[TDE] GET returned found=false for {domain}")
                return None
            print(f"[TDE] Step 2 done: full intel for {domain} (sections: {list((full.get('sections') or {}).keys())})")
            return _map_tde_company_full(full, domain)

    except httpx.TimeoutException:
        print(f"[TDE] Timeout (90s) researching {domain} — falling back to Tavily")
    except Exception as e:
        print(f"[TDE] Error researching {domain}: {e}")
    return None


async def _tde_research_industry(industry: str, sub_industry: str) -> Optional[IndustryProfile]:
    """
    POST /intel/research/industry — returns pain_points, trends, regulations in the response body.
    Single call, no need for a separate GET.
    """
    if not TDE_URL:
        return None
    payload = {"industry": industry, "sub_industry": sub_industry or ""}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{TDE_URL}/intel/research/industry",
                json=payload, headers=_tde_headers(),
            )
            if r.status_code == 200:
                data = r.json()
                print(f"[TDE] industry source={data.get('source','?')} for {industry}")
                return _map_tde_industry(data, industry, sub_industry)
            print(f"[TDE] POST industry HTTP {r.status_code} for {industry}: {r.text[:200]}")
    except Exception as e:
        print(f"[TDE] industry error for {industry}: {e}")
    return None


# ──────────────────────────────────────────────────────────
# Tavily fallback
# ──────────────────────────────────────────────────────────

async def _search(query: str, max_results: int = 5) -> list:
    if not tavily:
        return []
    try:
        resp = await tavily.search(
            query=query, max_results=max_results,
            search_depth="basic", include_answer=True,
        )
        results = resp.get("results", [])
        answer = resp.get("answer", "")
        if answer:
            results.insert(0, {"title": "Summary", "content": answer, "url": ""})
        return results
    except Exception as e:
        return [{"title": "Error", "content": str(e), "url": ""}]


def _fmt(results: list) -> str:
    lines = []
    for r in results:
        lines.append(f"[{r.get('title','')}] {r.get('url','')}")
        lines.append(r.get("content", "")[:600])
        lines.append("")
    return "\n".join(lines)


async def _company_searches(domain: str) -> str:
    hint = domain.split(".")[0].title()
    queries = [
        f"{hint} {domain} company overview what do they do",
        f"{hint} {domain} employees revenue funding size",
        f"{domain} site:linkedin.com/company",
        f"{hint} {domain} recent news 2024 2025",
        f"{hint} technology stack tools used",
    ]
    results = await asyncio.gather(*[_search(q, 4) for q in queries])
    return "\n\n".join(f"=== {q} ===\n{_fmt(r)}" for q, r in zip(queries, results))


async def _industry_searches(industry: str) -> str:
    queries = [
        f"{industry} industry biggest pain points challenges 2025",
        f"{industry} software buying triggers decision criteria",
        f"{industry} common objections technology adoption",
        f"{industry} industry trends disruptions 2025",
    ]
    results = await asyncio.gather(*[_search(q, 4) for q in queries])
    return "\n\n".join(f"=== {q} ===\n{_fmt(r)}" for q, r in zip(queries, results))


def _company_prompt(domain: str, research: str) -> str:
    schema = (
        '{\n'
        '  "domain": "' + domain + '",\n'
        '  "company_name": "string",\n'
        '  "description": "1-2 sentence description",\n'
        '  "industry": "e.g. Healthcare, Manufacturing, SaaS, Retail",\n'
        '  "sub_industry": "more specific vertical",\n'
        '  "company_size": "1-10 | 11-50 | 51-200 | 201-1000 | 1000+",\n'
        '  "estimated_revenue": "e.g. $5M-$20M or unknown",\n'
        '  "founded_year": "integer or null",\n'
        '  "hq_location": "City, State/Country",\n'
        '  "business_model": "B2B SaaS | Services | Marketplace | B2C | Other",\n'
        '  "tech_stack_signals": ["list"],\n'
        '  "recent_news": ["up to 3 events, 1 sentence each"],\n'
        '  "funding_stage": "Bootstrapped|Pre-Seed|Seed|Series A|Series B|Series C+|Public|Unknown",\n'
        '  "key_competitors": ["up to 4"],\n'
        '  "linkedin_url": "url or empty string",\n'
        '  "confidence": "low | medium | high",\n'
        '  "research_notes": "brief caveat on data quality"\n'
        '}'
    )
    return (
        "You are a B2B sales intelligence analyst.\n"
        f"Analyze this web research about the company at domain: {domain}\n\n"
        f"<research>\n{research}\n</research>\n\n"
        "Return a JSON object matching this schema exactly. "
        "Use null for unknown fields; infer estimates when evidence supports it.\n\n"
        f"{schema}\n\n"
        "Return ONLY valid JSON. No markdown, no explanation."
    )


def _industry_prompt(industry: str, sub_industry: str, research: str) -> str:
    schema = (
        '{\n'
        f'  "industry": "{industry}",\n'
        f'  "sub_industry": "{sub_industry}",\n'
        '  "top_pain_points": ["5-7 specific, concrete pains"],\n'
        '  "buying_triggers": ["4-6 events that make a company ready to buy"],\n'
        '  "common_objections": ["4-5 typical buyer objections"],\n'
        '  "key_metrics_they_care_about": ["5-7 KPIs"],\n'
        '  "industry_trends": ["3-5 trends in 2024-2025"],\n'
        '  "regulatory_pressures": ["2-4 compliance/legal pressures"],\n'
        '  "typical_decision_makers": ["titles in order of influence"],\n'
        '  "average_sales_cycle": "e.g. 30-60 days"\n'
        '}'
    )
    return (
        "You are a B2B sales intelligence analyst.\n"
        f"Industry: {industry}  Sub-industry: {sub_industry}\n\n"
        f"<research>\n{research}\n</research>\n\n"
        "Return a JSON object matching this schema. Be specific — generic answers are useless.\n\n"
        f"{schema}\n\n"
        "Return ONLY valid JSON. No markdown, no explanation."
    )


async def _synthesize_company(domain: str, raw: str) -> CompanyProfile:
    resp = await openrouter.chat.completions.create(
        model=OPENROUTER_MODEL, max_tokens=1200,
        messages=[{"role": "user", "content": _company_prompt(domain, raw)}],
    )
    text = resp.choices[0].message.content.strip().lstrip("```json").lstrip("```").rstrip("```")
    return CompanyProfile(**json.loads(text))


async def _synthesize_industry(industry: str, sub_industry: str, raw: str) -> IndustryProfile:
    resp = await openrouter.chat.completions.create(
        model=OPENROUTER_MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": _industry_prompt(industry, sub_industry, raw)}],
    )
    text = resp.choices[0].message.content.strip().lstrip("```json").lstrip("```").rstrip("```")
    return IndustryProfile(**json.loads(text))


async def _tavily_research(domain: str):
    """Tavily + OpenRouter fallback pipeline."""
    company_raw = await _company_searches(domain)
    company_profile = await _synthesize_company(domain, company_raw)
    industry_raw = await _industry_searches(company_profile.industry or "Technology")
    industry_profile = await _synthesize_industry(
        company_profile.industry or "Technology",
        company_profile.sub_industry or "",
        industry_raw,
    )
    return company_profile, industry_profile


# ──────────────────────────────────────────────────────────
# Sales context resolver
# ──────────────────────────────────────────────────────────

def resolve_sales_context(result) -> SalesContext:
    c = result.company
    i = result.industry
    conf = c.confidence

    src_label = (
        "TDE (live)"
        if result.source == "tde"
        else "web search"
    )

    base = dict(
        domain=c.domain,
        confidence=conf,
        company_name=c.company_name,
        company_description=c.description,
        company_size=c.company_size,
        company_revenue=c.estimated_revenue,
        company_funding=c.funding_stage,
        company_hq=c.hq_location,
        company_business_model=c.business_model,
        company_tech_stack=c.tech_stack_signals,
        company_recent_news=c.recent_news,
        company_competitors=c.key_competitors,
        industry=c.industry or i.industry,
        sub_industry=c.sub_industry or i.sub_industry,
        industry_trends=i.industry_trends,
        regulatory_pressures=i.regulatory_pressures,
        pain_points=i.top_pain_points,
        buying_triggers=i.buying_triggers,
        common_objections=i.common_objections,
        key_metrics=i.key_metrics_they_care_about,
        typical_decision_makers=i.typical_decision_makers,
        average_sales_cycle=i.average_sales_cycle,
    )

    if conf == "high":
        base["primary_source"] = "company"
        base["context_note"] = (
            f"Strong intel on {c.company_name} via {src_label}. "
            f"Reference their context ({c.business_model}, {c.company_size}, "
            f"{c.funding_stage}) when framing pain points."
        )
    elif conf == "medium":
        confirmed = [x for x in [
            c.company_size and f"{c.company_size} employees",
            c.funding_stage not in ("", "Unknown") and c.funding_stage,
            c.business_model or "",
            c.hq_location and f"based in {c.hq_location}",
        ] if x]
        confirmed_str = ", ".join(confirmed) if confirmed else "company name only"
        base["primary_source"] = "blended"
        base["context_note"] = (
            f"Partial intel for {c.company_name or c.domain} via {src_label}. "
            f"Confirmed: {confirmed_str}. "
            f"Fill gaps with {i.industry} benchmarks. "
            "Use discovery questions to confirm specifics."
        )
    else:
        base["primary_source"] = "industry"
        base["context_note"] = (
            f"Limited public info for {c.domain} via {src_label}. "
            f"Relying on {i.industry} industry benchmarks. "
            "Treat pain points as hypotheses to confirm."
        )

    return SalesContext(**base)


# ──────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────

async def research_company(domain: str) -> ResearchResult:
    """
    Full pipeline: domain → ResearchResult with sales_context.

    1. Try TDE (cache-first, then research swarm):
       - POST /intel/research/company  (up to 90s for fresh research)
       - GET  /intel/company/:domain   (fast DB read for full data)
    2. Fall back to Tavily + OpenRouter if TDE fails.
    """
    start = datetime.utcnow()
    source = "tavily"

    company_profile: Optional[CompanyProfile] = None
    industry_profile: Optional[IndustryProfile] = None

    if TDE_URL:
        company_profile = await _tde_research_company(domain)
        if company_profile:
            source = "tde"
            industry_profile = await _tde_research_industry(
                company_profile.industry or "Technology",
                company_profile.sub_industry or "",
            )

    if not company_profile:
        print(f"[research] TDE unavailable — falling back to Tavily for {domain}")
        company_profile, industry_profile = await _tavily_research(domain)
        source = "tavily"

    if not industry_profile:
        industry_raw = await _industry_searches(company_profile.industry or "Technology")
        industry_profile = await _synthesize_industry(
            company_profile.industry or "Technology",
            company_profile.sub_industry or "",
            industry_raw,
        )

    result = ResearchResult(
        domain=domain,
        company=company_profile,
        industry=industry_profile,
        duration_seconds=round((datetime.utcnow() - start).total_seconds(), 2),
        source=source,
    )
    result.sales_context = resolve_sales_context(result)
    return result


# ──────────────────────────────────────────────────────────
# Utility
# ──────────────────────────────────────────────────────────

def domain_from_email(email: str) -> str:
    FREE = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "icloud.com", "aol.com", "protonmail.com", "me.com",
    }
    try:
        d = email.strip().lower().split("@")[1]
        return "" if d in FREE else d
    except IndexError:
        return ""
