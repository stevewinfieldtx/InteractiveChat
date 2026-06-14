-- schema.sql
-- Run once against your Railway PostgreSQL instance

CREATE TABLE IF NOT EXISTS company_profiles (
    id                SERIAL PRIMARY KEY,
    domain            TEXT NOT NULL UNIQUE,
    company_name      TEXT,
    description       TEXT,
    industry          TEXT,
    sub_industry      TEXT,
    company_size      TEXT,
    estimated_revenue TEXT,
    founded_year      INT,
    hq_location       TEXT,
    business_model    TEXT,
    tech_stack_signals  JSONB DEFAULT '[]',
    recent_news         JSONB DEFAULT '[]',
    funding_stage     TEXT,
    key_competitors   JSONB DEFAULT '[]',
    linkedin_url      TEXT,
    confidence        TEXT DEFAULT 'low',
    research_notes    TEXT,
    researched_at     TIMESTAMPTZ DEFAULT NOW(),
    expires_at        TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '30 days')
);

CREATE INDEX IF NOT EXISTS idx_company_profiles_domain   ON company_profiles(domain);
CREATE INDEX IF NOT EXISTS idx_company_profiles_industry ON company_profiles(industry);

CREATE TABLE IF NOT EXISTS industry_profiles (
    id              SERIAL PRIMARY KEY,
    industry        TEXT NOT NULL,
    sub_industry    TEXT NOT NULL DEFAULT '',
    UNIQUE (industry, sub_industry),
    top_pain_points          JSONB DEFAULT '[]',
    buying_triggers          JSONB DEFAULT '[]',
    common_objections        JSONB DEFAULT '[]',
    key_metrics              JSONB DEFAULT '[]',
    industry_trends          JSONB DEFAULT '[]',
    regulatory_pressures     JSONB DEFAULT '[]',
    typical_decision_makers  JSONB DEFAULT '[]',
    average_sales_cycle      TEXT,
    researched_at   TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '14 days')
);

CREATE INDEX IF NOT EXISTS idx_industry_profiles_industry ON industry_profiles(industry);

CREATE TABLE IF NOT EXISTS research_jobs (
    id              SERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL,
    domain          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    duration_seconds FLOAT,
    error_message   TEXT,
    company_profile_id  INT REFERENCES company_profiles(id),
    industry_profile_id INT REFERENCES industry_profiles(id)
);

CREATE INDEX IF NOT EXISTS idx_research_jobs_session ON research_jobs(session_id);
CREATE INDEX IF NOT EXISTS idx_research_jobs_domain  ON research_jobs(domain);
CREATE INDEX IF NOT EXISTS idx_research_jobs_status  ON research_jobs(status);

CREATE TABLE IF NOT EXISTS conversations (
    id              SERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL UNIQUE,
    solution_slug   TEXT NOT NULL,
    contact_name    TEXT,
    contact_email   TEXT,
    contact_title   TEXT,
    domain          TEXT,
    research_job_id INT REFERENCES research_jobs(id),
    messages        JSONB DEFAULT '[]',
    outcome         TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_session  ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_conversations_email    ON conversations(contact_email);
CREATE INDEX IF NOT EXISTS idx_conversations_outcome  ON conversations(outcome);

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_conversations_updated
    BEFORE UPDATE ON conversations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Partner leads — every reseller who calls in, for follow-up + engagement BI
CREATE TABLE IF NOT EXISTS partner_leads (
    id                SERIAL PRIMARY KEY,
    session_id        TEXT UNIQUE,
    partner_name      TEXT,
    partner_company   TEXT,
    partner_email     TEXT,
    partner_phone     TEXT,
    customer_vertical TEXT,
    last_signal       TEXT,
    signal_count      INT DEFAULT 0,
    handed_off        BOOLEAN DEFAULT FALSE,
    brief             TEXT,
    transcript        JSONB DEFAULT '[]',
    first_seen        TIMESTAMPTZ DEFAULT NOW(),
    last_seen         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_partner_leads_email   ON partner_leads(partner_email);
CREATE INDEX IF NOT EXISTS idx_partner_leads_company ON partner_leads(partner_company);
CREATE INDEX IF NOT EXISTS idx_partner_leads_seen    ON partner_leads(last_seen DESC);
