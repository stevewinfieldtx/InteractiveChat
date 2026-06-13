import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import { nanoid } from 'nanoid';
import { db, touchSession } from './db.js';
import { openRouterJson } from './openrouter.js';
import { researchCompanyFromLead } from './research.js';

const app = express();
app.use(cors());
app.use(express.json({ limit: '2mb' }));

function logEvent(sessionId, type, payload) {
  db.prepare('INSERT INTO voice_events (id, session_id, type, payload) VALUES (?, ?, ?, ?)')
    .run(nanoid(), sessionId, type, JSON.stringify(payload || {}));
  touchSession(sessionId);
}

function getSession(sessionId) {
  return db.prepare('SELECT * FROM voice_sessions WHERE id = ?').get(sessionId);
}

function upsertLead(sessionId, fields) {
  const existing = db.prepare('SELECT * FROM voice_leads WHERE session_id = ?').get(sessionId);
  if (!existing) {
    const id = nanoid();
    db.prepare(`
      INSERT INTO voice_leads (
        id, session_id, name, email, company, website, stated_need, verified_project_interest,
        intent, tone, urgency, product_interest, human_requested
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      id, sessionId,
      fields.name || null,
      fields.email || null,
      fields.company || null,
      fields.website || null,
      fields.stated_need || null,
      fields.verified_project_interest || null,
      fields.intent || null,
      fields.tone || null,
      fields.urgency || null,
      fields.product_interest || null,
      fields.human_requested ? 1 : 0
    );
    return db.prepare('SELECT * FROM voice_leads WHERE id = ?').get(id);
  }

  const merged = { ...existing, ...Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== undefined && v !== null && v !== '')) };
  db.prepare(`
    UPDATE voice_leads SET
      name = ?, email = ?, company = ?, website = ?, stated_need = ?, verified_project_interest = ?,
      intent = ?, tone = ?, urgency = ?, product_interest = ?, human_requested = ?, updated_at = CURRENT_TIMESTAMP
    WHERE session_id = ?
  `).run(
    merged.name || null,
    merged.email || null,
    merged.company || null,
    merged.website || null,
    merged.stated_need || null,
    merged.verified_project_interest || null,
    merged.intent || null,
    merged.tone || null,
    merged.urgency || null,
    merged.product_interest || null,
    merged.human_requested ? 1 : 0,
    sessionId
  );
  return db.prepare('SELECT * FROM voice_leads WHERE session_id = ?').get(sessionId);
}

function parseJsonColumn(value) {
  if (!value) return [];
  try { return JSON.parse(value); } catch { return []; }
}

function hydrateSession(row) {
  const lead = db.prepare('SELECT * FROM voice_leads WHERE session_id = ?').get(row.id) || null;
  const intelligence = db.prepare('SELECT * FROM voice_intelligence WHERE session_id = ? ORDER BY created_at DESC LIMIT 1').get(row.id) || null;
  const events = db.prepare('SELECT * FROM voice_events WHERE session_id = ? ORDER BY created_at ASC LIMIT 100').all(row.id)
    .map(e => ({ ...e, payload: JSON.parse(e.payload) }));

  if (intelligence) {
    intelligence.likely_pain_points = parseJsonColumn(intelligence.likely_pain_points);
    intelligence.engagement_strategy = parseJsonColumn(intelligence.engagement_strategy);
    intelligence.suggested_ai_questions = parseJsonColumn(intelligence.suggested_ai_questions);
    intelligence.suggested_human_questions = parseJsonColumn(intelligence.suggested_human_questions);
  }

  return { ...row, lead, intelligence, events };
}

app.get('/api/health', (req, res) => {
  res.json({ ok: true, service: 'website-voice-agent-backend' });
});

app.post('/api/voice/session-started', (req, res) => {
  const id = nanoid();
  const { page_url, project_name, project_context } = req.body || {};
  db.prepare(`
    INSERT INTO voice_sessions (id, status, page_url, project_name, project_context)
    VALUES (?, 'active', ?, ?, ?)
  `).run(id, page_url || null, project_name || null, project_context || null);
  logEvent(id, 'session_started', req.body || {});
  res.json({ session_id: id });
});

app.post('/api/voice/link-elevenlabs-conversation', (req, res) => {
  const { session_id, elevenlabs_conversation_id } = req.body || {};
  if (!session_id) return res.status(400).json({ error: 'session_id required' });
  db.prepare('UPDATE voice_sessions SET elevenlabs_conversation_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?')
    .run(elevenlabs_conversation_id || null, session_id);
  logEvent(session_id, 'elevenlabs_conversation_linked', { elevenlabs_conversation_id });
  res.json({ ok: true });
});

app.post('/api/voice/client-message', (req, res) => {
  const { session_id, message } = req.body || {};
  if (!session_id) return res.status(400).json({ error: 'session_id required' });
  logEvent(session_id, 'client_message', { message });
  res.json({ ok: true });
});

app.post('/api/voice/update-lead', async (req, res) => {
  const body = req.body || {};
  const sessionId = body.session_id;
  if (!sessionId) return res.status(400).json({ error: 'session_id required' });

  const session = getSession(sessionId);
  if (!session) return res.status(404).json({ error: 'session not found' });

  const lead = upsertLead(sessionId, {
    name: body.name,
    email: body.email,
    company: body.company,
    website: body.website,
    stated_need: body.stated_need || body.need,
    verified_project_interest: body.verified_project_interest,
    product_interest: body.product_interest
  });
  logEvent(sessionId, 'lead_updated', body);

  const companyInfo = body.website || body.email || body.company_url || body.companyInfo;
  let research = null;

  if (companyInfo) {
    researchCompanyFromLead({
      sessionId,
      companyInfo,
      statedNeed: lead.stated_need,
      projectName: session.project_name,
      projectContext: session.project_context
    }).then((result) => {
      logEvent(sessionId, 'company_research_complete', { sourceUrl: result.sourceUrl, confidence: result.confidence });
    }).catch((error) => {
      logEvent(sessionId, 'company_research_error', { error: error.message });
    });

    research = { status: 'started', message: 'Company research started immediately.' };
  }

  res.json({ ok: true, lead, research });
});

const intentSchema = {
  name: 'live_intent_analysis',
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: {
      intent: { type: 'string' },
      tone: { type: 'string' },
      urgency: { type: 'string' },
      product_interest: { type: 'string' },
      verified_project_interest: { type: 'string' },
      human_requested: { type: 'boolean' },
      human_needed_reason: { type: 'string' },
      suggested_next_question: { type: 'string' }
    },
    required: ['intent', 'tone', 'urgency', 'product_interest', 'verified_project_interest', 'human_requested', 'human_needed_reason', 'suggested_next_question']
  }
};

app.post('/api/voice/analyze-intent', async (req, res) => {
  const body = req.body || {};
  const sessionId = body.session_id;
  if (!sessionId) return res.status(400).json({ error: 'session_id required' });
  const session = getSession(sessionId);
  if (!session) return res.status(404).json({ error: 'session not found' });

  try {
    const analysis = await openRouterJson({
      model: process.env.OPENROUTER_MODEL_ANALYSIS || 'openai/gpt-4o-mini',
      system: 'You classify live website voice-agent conversations. Return JSON only. Be concise and practical.',
      user: `
PROJECT PAGE CONTEXT:
${session.project_context || ''}

LATEST CUSTOMER MESSAGE:
${body.latest_customer_message || ''}

CONVERSATION SUMMARY:
${body.conversation_summary || ''}

Decide intent, tone, urgency, likely product/project interest, whether a human is needed, and the next useful question.
`,
      schema: intentSchema,
      temperature: 0.1
    });

    upsertLead(sessionId, analysis);
    logEvent(sessionId, 'intent_analyzed', analysis);
    res.json({ ok: true, analysis });
  } catch (error) {
    logEvent(sessionId, 'intent_analysis_error', { error: error.message });
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/voice/request-human', (req, res) => {
  const { session_id, reason, suggested_opener } = req.body || {};
  if (!session_id) return res.status(400).json({ error: 'session_id required' });
  upsertLead(session_id, { human_requested: true });
  logEvent(session_id, 'human_requested', { reason, suggested_opener });
  res.json({ ok: true, message: 'Human request logged. Wire this to SMS/Slack/email next.' });
});

app.post('/api/voice/conversation-ended', (req, res) => {
  const { session_id, transcript, summary } = req.body || {};
  if (!session_id) return res.status(400).json({ error: 'session_id required' });
  db.prepare("UPDATE voice_sessions SET status = 'ended', ended_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?")
    .run(session_id);
  logEvent(session_id, 'conversation_ended', { transcript, summary });
  res.json({ ok: true });
});

// Mint a short-lived ElevenLabs conversation token (server-side, uses the secret API key).
// This lets the agent be PRIVATE — the browser never sees the API key.
app.get('/api/voice/token', async (req, res) => {
  const agentId = req.query.agent_id || process.env.ELEVENLABS_AGENT_ID;
  if (!process.env.ELEVENLABS_API_KEY) {
    return res.status(500).json({ error: 'ELEVENLABS_API_KEY is missing in backend/.env' });
  }
  if (!agentId) return res.status(400).json({ error: 'agent_id required' });
  try {
    const r = await fetch(
      `https://api.elevenlabs.io/v1/convai/conversation/token?agent_id=${encodeURIComponent(agentId)}`,
      { headers: { 'xi-api-key': process.env.ELEVENLABS_API_KEY } }
    );
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return res.status(r.status).json({ error: data?.detail?.message || data?.detail || JSON.stringify(data) });
    }
    res.json({ token: data.token });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/research/run', async (req, res) => {
  const { session_id, company_info } = req.body || {};
  if (!session_id || !company_info) return res.status(400).json({ error: 'session_id and company_info required' });
  const session = getSession(session_id);
  if (!session) return res.status(404).json({ error: 'session not found' });
  const lead = db.prepare('SELECT * FROM voice_leads WHERE session_id = ?').get(session_id) || {};
  try {
    const result = await researchCompanyFromLead({
      sessionId: session_id,
      companyInfo: company_info,
      statedNeed: lead.stated_need,
      projectName: session.project_name,
      projectContext: session.project_context
    });
    logEvent(session_id, 'manual_research_complete', result);
    res.json({ ok: true, result });
  } catch (error) {
    logEvent(session_id, 'manual_research_error', { error: error.message });
    res.status(500).json({ error: error.message });
  }
});

app.get('/api/dashboard/sessions', (req, res) => {
  const rows = db.prepare('SELECT * FROM voice_sessions ORDER BY created_at DESC LIMIT 50').all();
  res.json(rows.map(hydrateSession));
});

app.get('/api/dashboard/sessions/:id', (req, res) => {
  const row = getSession(req.params.id);
  if (!row) return res.status(404).json({ error: 'not found' });
  res.json(hydrateSession(row));
});

const port = Number(process.env.PORT || 8787);
app.listen(port, () => {
  console.log(`Voice agent backend running on http://localhost:${port}`);
});
