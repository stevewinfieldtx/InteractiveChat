import { parse } from 'node-html-parser';
import { nanoid } from 'nanoid';
import { db } from './db.js';
import { openRouterJson } from './openrouter.js';

function normalizeUrl(input) {
  if (!input) return null;
  let value = String(input).trim();
  const emailMatch = value.match(/[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})/i);
  if (emailMatch) value = emailMatch[1];
  value = value.replace(/^mailto:/i, '').replace(/^https?:\/\//i, '').split('/')[0];
  if (!value || !value.includes('.')) return null;
  return `https://${value}`;
}

function cleanText(text) {
  return text.replace(/\s+/g, ' ').trim().slice(0, 12000);
}

async function fetchHomepage(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: {
        'User-Agent': 'Mozilla/5.0 WebsiteVoiceAgentResearch/0.1'
      }
    });
    if (!response.ok) throw new Error(`Fetch ${response.status}`);
    const html = await response.text();
    const root = parse(html);
    root.querySelectorAll('script, style, noscript, svg').forEach((node) => node.remove());
    const title = root.querySelector('title')?.text || '';
    const metaDescription = root.querySelector('meta[name="description"]')?.getAttribute('content') || '';
    const h1 = root.querySelector('h1')?.text || '';
    const body = cleanText(root.text);
    return cleanText([title, metaDescription, h1, body].filter(Boolean).join('\n'));
  } finally {
    clearTimeout(timer);
  }
}

const researchSchema = {
  name: 'company_research_strategy',
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: {
      company_summary: { type: 'string' },
      website_summary: { type: 'string' },
      likely_industry: { type: 'string' },
      likely_company_size: { type: 'string' },
      likely_pain_points: { type: 'array', items: { type: 'string' } },
      engagement_strategy: { type: 'array', items: { type: 'string' } },
      suggested_ai_questions: { type: 'array', items: { type: 'string' } },
      suggested_human_questions: { type: 'array', items: { type: 'string' } },
      suggested_human_opener: { type: 'string' },
      handoff_summary: { type: 'string' },
      confidence: { type: 'integer', minimum: 1, maximum: 5 }
    },
    required: [
      'company_summary',
      'website_summary',
      'likely_industry',
      'likely_company_size',
      'likely_pain_points',
      'engagement_strategy',
      'suggested_ai_questions',
      'suggested_human_questions',
      'suggested_human_opener',
      'handoff_summary',
      'confidence'
    ]
  }
};

export async function researchCompanyFromLead({ sessionId, companyInfo, statedNeed, projectName, projectContext }) {
  const sourceUrl = normalizeUrl(companyInfo);
  if (!sourceUrl) return { skipped: true, reason: 'No usable company URL or email domain yet' };

  const intelligenceId = nanoid();
  db.prepare(`
    INSERT INTO voice_intelligence (id, session_id, source_url, status)
    VALUES (?, ?, ?, 'running')
  `).run(intelligenceId, sessionId, sourceUrl);

  try {
    const homepageText = await fetchHomepage(sourceUrl);

    const system = `You are a sales strategist. Analyze a prospect company quickly and map likely pain points to the project the visitor is asking about. Be practical. No fluff. Return JSON only.`;

    const user = `
PROJECT NAME:
${projectName || 'Unknown project'}

PROJECT / PAGE CONTEXT:
${projectContext || 'Unknown'}

VISITOR STATED NEED:
${statedNeed || 'Not yet known'}

COMPANY URL:
${sourceUrl}

HOMEPAGE TEXT:
${homepageText}

TASK:
Infer the company type, likely pain points, engagement strategy, and the next questions the website voice agent should ask. The visitor is already on the project page, so assume the project context is a strong clue, but verify fit through questions.
`;

    const result = await openRouterJson({
      model: process.env.OPENROUTER_MODEL_RESEARCH || 'openai/gpt-4o-mini',
      system,
      user,
      schema: researchSchema,
      temperature: 0.15
    });

    db.prepare(`
      UPDATE voice_intelligence SET
        company_summary = ?,
        website_summary = ?,
        likely_industry = ?,
        likely_company_size = ?,
        likely_pain_points = ?,
        engagement_strategy = ?,
        suggested_ai_questions = ?,
        suggested_human_questions = ?,
        suggested_human_opener = ?,
        handoff_summary = ?,
        confidence = ?,
        status = 'complete',
        updated_at = CURRENT_TIMESTAMP
      WHERE id = ?
    `).run(
      result.company_summary,
      result.website_summary,
      result.likely_industry,
      result.likely_company_size,
      JSON.stringify(result.likely_pain_points),
      JSON.stringify(result.engagement_strategy),
      JSON.stringify(result.suggested_ai_questions),
      JSON.stringify(result.suggested_human_questions),
      result.suggested_human_opener,
      result.handoff_summary,
      result.confidence,
      intelligenceId
    );

    return { intelligenceId, sourceUrl, ...result };
  } catch (error) {
    db.prepare(`
      UPDATE voice_intelligence SET status = 'error', error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
    `).run(error.message, intelligenceId);
    throw error;
  }
}
