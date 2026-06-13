import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Conversation } from '@elevenlabs/client';
import './styles.css';

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8787';
const AGENT_ID = import.meta.env.VITE_ELEVENLABS_AGENT_ID;
const PROJECT_NAME = import.meta.env.VITE_PROJECT_NAME || 'Website Voice Agent';
const PROJECT_CONTEXT = import.meta.env.VITE_PROJECT_PAGE_CONTEXT || 'Website voice agent with human handoff and company research.';

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function VoicePage() {
  const [status, setStatus] = useState('idle');
  const [mode, setMode] = useState('listening');
  const [sessionId, setSessionId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [error, setError] = useState(null);
  const conversationRef = useRef(null);

  async function startVoice() {
    setError(null);
    setStatus('starting');

    if (!AGENT_ID) {
      setError('Missing VITE_ELEVENLABS_AGENT_ID in .env');
      setStatus('idle');
      return;
    }

    try {
      await navigator.mediaDevices.getUserMedia({ audio: true });
      const created = await api('/api/voice/session-started', {
        method: 'POST',
        body: JSON.stringify({
          page_url: window.location.href,
          project_name: PROJECT_NAME,
          project_context: PROJECT_CONTEXT
        })
      });

      setSessionId(created.session_id);

      // Ask our backend for a short-lived ElevenLabs token (keeps the API key server-side).
      const { token } = await api(`/api/voice/token?agent_id=${encodeURIComponent(AGENT_ID)}`);
      if (!token) {
        throw new Error('Backend returned no ElevenLabs token. Check ELEVENLABS_API_KEY in backend/.env and that the agent belongs to that account.');
      }

      // NOTE: prompt/first-message overrides and dynamic variables are intentionally
      // omitted for now so the session connects cleanly using the agent's own
      // configuration. Re-add them once override fields are enabled in the agent's
      // ElevenLabs security settings.
      const conversation = await Conversation.startSession({
        conversationToken: token,
        connectionType: 'webrtc',
        onConnect: async () => {
          setStatus('connected');
          const elevenlabsId = conversation.getId?.();
          await api('/api/voice/link-elevenlabs-conversation', {
            method: 'POST',
            body: JSON.stringify({
              session_id: created.session_id,
              elevenlabs_conversation_id: elevenlabsId
            })
          }).catch(() => {});
        },
        onDisconnect: async () => {
          setStatus('ended');
          await api('/api/voice/conversation-ended', {
            method: 'POST',
            body: JSON.stringify({ session_id: created.session_id })
          }).catch(() => {});
        },
        onMessage: async (message) => {
          setMessages((prev) => [...prev, message]);
          await api('/api/voice/client-message', {
            method: 'POST',
            body: JSON.stringify({ session_id: created.session_id, message })
          }).catch(() => {});
        },
        onError: (err) => {
          setError(String(err?.message || err));
          setStatus('error');
        },
        onStatusChange: ({ status }) => setStatus(status),
        onModeChange: ({ mode }) => setMode(mode)
      });

      conversationRef.current = conversation;
    } catch (err) {
      setError(err.message || String(err));
      setStatus('idle');
    }
  }

  async function endVoice() {
    await conversationRef.current?.endSession?.();
    conversationRef.current = null;
    setStatus('ended');
  }

  return (
    <main className="page">
      <section className="hero">
        <div className="badge">Browser voice agent MVP</div>
        <h1>Talk it through right here.</h1>
        <p>
          Start a live voice conversation in your browser. The assistant will understand what you need,
          capture your company context, and prepare the team with the details.
        </p>
        <div className="actions">
          {status === 'idle' || status === 'ended' || status === 'error' ? (
            <button className="primary" onClick={startVoice}>Talk Now</button>
          ) : (
            <button className="danger" onClick={endVoice}>End Voice Chat</button>
          )}
          <a className="secondary" href="/dashboard">Open Human Dashboard</a>
        </div>

        {status === 'connected' && (
          <div className="voicestate" style={{ marginTop: 14, display: 'flex', alignItems: 'center', gap: 10, fontWeight: 600 }}>
            <span style={{ width: 12, height: 12, borderRadius: '50%', background: mode === 'speaking' ? '#22d3ee' : '#34d399', boxShadow: `0 0 12px ${mode === 'speaking' ? '#22d3ee' : '#34d399'}` }} />
            {mode === 'speaking' ? 'Agent is speaking…' : 'Listening — go ahead and talk'}
          </div>
        )}
        {(status === 'starting' || status === 'connecting') && (
          <div className="voicestate" style={{ marginTop: 14, fontWeight: 600 }}>Connecting… allow the microphone prompt if it appears.</div>
        )}
        <p className="fineprint">
          This starts a browser voice session. Microphone permission is required. Conversations may be processed to answer your request and support follow-up.
        </p>
        {error && <div className="error">{error}</div>}
      </section>

      <section className="panel">
        <h2>Session</h2>
        <div className="kv"><span>Status</span><strong>{status}</strong></div>
        <div className="kv"><span>Session ID</span><strong>{sessionId || 'none yet'}</strong></div>
        <h3>Client events</h3>
        <div className="log">
          {messages.length === 0 ? <em>No messages yet.</em> : messages.slice(-12).map((m, i) => (
            <pre key={i}>{JSON.stringify(m, null, 2)}</pre>
          ))}
        </div>
      </section>
    </main>
  );
}

function Dashboard() {
  const [sessions, setSessions] = useState([]);
  const [selected, setSelected] = useState(null);

  async function load() {
    const data = await api('/api/dashboard/sessions');
    setSessions(data);
    if (!selected && data[0]) setSelected(data[0]);
    if (selected) {
      const refreshed = data.find((s) => s.id === selected.id);
      if (refreshed) setSelected(refreshed);
    }
  }

  useEffect(() => {
    load();
    const timer = setInterval(load, 2500);
    return () => clearInterval(timer);
  }, [selected?.id]);

  return (
    <main className="dash">
      <aside className="sidebar">
        <h2>Voice Sessions</h2>
        <a href="/" className="smalllink">← Voice page</a>
        {sessions.map((s) => (
          <button key={s.id} className={`sessionBtn ${selected?.id === s.id ? 'active' : ''}`} onClick={() => setSelected(s)}>
            <strong>{s.lead?.company || s.lead?.name || 'Unknown visitor'}</strong>
            <span>{s.lead?.intent || s.status}</span>
            {s.lead?.human_requested ? <b>Human requested</b> : null}
          </button>
        ))}
      </aside>

      <section className="detail">
        {!selected ? <p>No sessions yet.</p> : <SessionDetail session={selected} />}
      </section>
    </main>
  );
}

function SessionDetail({ session }) {
  const intel = session.intelligence;
  const lead = session.lead;
  return (
    <>
      <div className="topline">
        <h1>{lead?.company || lead?.name || 'Unknown visitor'}</h1>
        <span className={`pill ${lead?.human_requested ? 'hot' : ''}`}>{lead?.human_requested ? 'Human requested' : session.status}</span>
      </div>

      <div className="grid">
        <Card title="Lead">
          <Line label="Name" value={lead?.name} />
          <Line label="Email" value={lead?.email} />
          <Line label="Company" value={lead?.company} />
          <Line label="Website" value={lead?.website} />
          <Line label="Need" value={lead?.stated_need} />
          <Line label="Intent" value={lead?.intent} />
          <Line label="Tone" value={lead?.tone} />
          <Line label="Urgency" value={lead?.urgency} />
        </Card>

        <Card title="Company Intelligence">
          {!intel ? <p>No research yet. It starts when URL/email is known.</p> : (
            <>
              <Line label="Status" value={intel.status} />
              <Line label="Source" value={intel.source_url} />
              <Line label="Industry" value={intel.likely_industry} />
              <Line label="Size" value={intel.likely_company_size} />
              <p>{intel.company_summary}</p>
              {intel.error && <p className="error">{intel.error}</p>}
            </>
          )}
        </Card>

        <Card title="Likely Pain Points">
          <List items={intel?.likely_pain_points} />
        </Card>

        <Card title="Engagement Strategy">
          <List items={intel?.engagement_strategy} />
        </Card>

        <Card title="Questions for AI">
          <List items={intel?.suggested_ai_questions} />
        </Card>

        <Card title="Questions for Human">
          <List items={intel?.suggested_human_questions} />
        </Card>
      </div>

      <Card title="Human Opener">
        <p className="opener">{intel?.suggested_human_opener || 'Waiting for research.'}</p>
      </Card>

      <Card title="Events">
        <div className="events">
          {session.events?.slice().reverse().map((e) => (
            <div key={e.id} className="event"><strong>{e.type}</strong><pre>{JSON.stringify(e.payload, null, 2)}</pre></div>
          ))}
        </div>
      </Card>
    </>
  );
}

function Card({ title, children }) {
  return <section className="card"><h3>{title}</h3>{children}</section>;
}
function Line({ label, value }) {
  return <div className="line"><span>{label}</span><strong>{value || '—'}</strong></div>;
}
function List({ items }) {
  if (!items || items.length === 0) return <p>Waiting.</p>;
  return <ul>{items.map((item, i) => <li key={i}>{item}</li>)}</ul>;
}

function App() {
  const isDashboard = window.location.pathname.startsWith('/dashboard');
  return isDashboard ? <Dashboard /> : <VoicePage />;
}

createRoot(document.getElementById('root')).render(<App />);
