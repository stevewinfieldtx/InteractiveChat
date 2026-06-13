"""
company_research.py
-------------------
Research agent: domain -> parallel web searches -> CompanyProfile + IndustryProfile
-> SalesContext (confidence-based fallback: low confidence leans on industry data).

Env vars: OPENROUTER_API_KEY, OPENROUTER_MODEL, TAVILY_API_KEY
"""

import asyncio
import json
import os
from datetime import datetime
from typing import Optional

from openai import AsyncOpenAI
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from tavily import AsyncTavilyClient

load_dotenv()

openrouter = AsyncOpenAI(
    api_key=os.getenv("OPENROUTER_API_KEY"),
    base_url="https://openrouter.ai/api/v1",
)
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4-5")
tavily = AsyncTavilyClient(api_key=os.getenv("TAVILY_API_KEY"))


# ──────────────────────────────────────────────────────────
# Models
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
    confidence: str = "low"   # low | medium | high
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


# ──────────────────────────────────────────────────────────
# Context resolver
# ──────────────────────────────────────────────────────────

def resolve_sales_context(result: ResearchResult) -> SalesContext:
    c = result.company
    i = result.industry
    conf = c.confidence

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
            f"Strong intel on {c.company_name}. "
            f"Reference their context ({c.business_model}, {c.company_size} employees, "
            f"{c.funding_stage}) when framing pain points and questions. "
            f"Industry benchmarks below apply — personalize them to what you know."
        )
    elif conf == "medium":
        known = [f for f in [
            c.company_size and f"{c.company_size} employees",
            c.funding_stage not in ("", "Unknown") and c.funding_stage,
            c.business_model or "",
            c.hq_location and f"based in {c.hq_location}",
        ] if f]
        base["primary_source"] = "blended"
        base["context_note"] = (
            f"Partial intel for {c.company_name or c.domain}. "
            f"Confirmed: {', '.join(known) or 'company name only'}. "
            f"Fill gaps with {i.industry} industry benchmarks. "
            f"Use discovery questions to confirm specifics rather than assuming."
        )
    else:
        base["primary_source"] = "industry"
        base["context_note"] = (
            f"Limited public info found for {c.domain}. "
            f"Relying on {i.industry} industry benchmarks as the primary anchor. "
            f"Use open discovery questions — treat pain points below as hypotheses to confirm, not facts."
        )

    return SalesContext(**base)


# ──────────────────────────────────────────────────────────
# Search helpers
# ──────────────────────────────────────────────────────────

async def _search(query: str, max_results: int = 5) -> list[dict]:
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


def _fmt(results: list[dict]) -> str:
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


# ──────────────────────────────────────────────────────────
# Synthesis prompts
# ──────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────
# Synthesis calls
# ──────────────────────────────────────────────────────────

async def _synthesize_company(domain: str, raw: str) -> CompanyProfile:
    resp = await openrouter.chat.completions.create(
        model=OPENROUTER_MODEL,
        max_tokens=1200,
        messages=[{"role": "user", "content": _company_prompt(domain, raw)}],
    )
    text = resp.choices[0].message.content.strip().lstrip("```json").lstrip("```").rstrip("```")
    return CompanyProfile(**json.loads(text))


async def _synthesize_industry(industry: str, sub_industry: str, raw: str) -> IndustryProfile:
    resp = await openrouter.chat.completions.create(
        model=OPENROUTER_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": _industry_prompt(industry, sub_industry, raw)}],
    )
    text = resp.choices[0].message.content.strip().lstrip("```json").lstrip("```").rstrip("```")
    return IndustryProfile(**json.loads(text))


# ──────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────

async def research_company(domain: str) -> ResearchResult:
    """Full pipeline: domain -> ResearchResult with sales_context attached. ~5-12 seconds."""
    start = datetime.utcnow()

    company_raw = await _company_searches(domain)
    company_profile = await _synthesize_company(domain, company_raw)

    industry = company_profile.industry or "Technology"
    sub_industry = company_profile.sub_industry or ""
    industry_raw = await _industry_searches(industry)
    industry_profile = await _synthesize_industry(industry, sub_industry, industry_raw)

    result = ResearchResult(
        domain=domain,
        company=company_profile,
        industry=industry_profile,
        duration_seconds=round((datetime.utcnow() - start).total_seconds(), 2),
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
