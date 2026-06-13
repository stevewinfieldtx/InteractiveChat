import Database from 'better-sqlite3';
import fs from 'fs';
import path from 'path';

const dbPath = process.env.DATABASE_PATH || './data/voice-agent.db';
fs.mkdirSync(path.dirname(dbPath), { recursive: true });

export const db = new Database(dbPath);
db.pragma('journal_mode = WAL');

db.exec(`
CREATE TABLE IF NOT EXISTS voice_sessions (
  id TEXT PRIMARY KEY,
  elevenlabs_conversation_id TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  page_url TEXT,
  project_name TEXT,
  project_context TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ended_at TEXT
);

CREATE TABLE IF NOT EXISTS voice_leads (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  name TEXT,
  email TEXT,
  company TEXT,
  website TEXT,
  stated_need TEXT,
  verified_project_interest TEXT,
  intent TEXT,
  tone TEXT,
  urgency TEXT,
  product_interest TEXT,
  human_requested INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(session_id) REFERENCES voice_sessions(id)
);

CREATE TABLE IF NOT EXISTS voice_events (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(session_id) REFERENCES voice_sessions(id)
);

CREATE TABLE IF NOT EXISTS voice_intelligence (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  company_summary TEXT,
  website_summary TEXT,
  likely_industry TEXT,
  likely_company_size TEXT,
  likely_pain_points TEXT,
  engagement_strategy TEXT,
  suggested_ai_questions TEXT,
  suggested_human_questions TEXT,
  suggested_human_opener TEXT,
  handoff_summary TEXT,
  confidence INTEGER,
  source_url TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(session_id) REFERENCES voice_sessions(id)
);
`);

export function touchSession(sessionId) {
  db.prepare('UPDATE voice_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?').run(sessionId);
}
