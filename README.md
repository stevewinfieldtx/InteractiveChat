# Website Voice Agent Starter

This is a starter implementation for a website-based ElevenLabs voice agent with an OpenRouter intelligence layer.

It does **not** use phone calls. The visitor starts a browser voice session from the web page.

## What it does

- Starts a website voice session from a React/Vite widget.
- Creates a backend session record.
- Passes page/project context into the ElevenLabs session.
- Exposes server-tool endpoints for ElevenLabs to call during the conversation.
- Starts company research as soon as a company URL or work email is known.
- Uses OpenRouter to produce pain points, engagement strategy, suggested AI questions, and human handoff notes.
- Provides a simple human dashboard with live-ish updates through polling.

## Important setup

1. Create an ElevenLabs Conversational AI agent.
2. Make it public for MVP testing or configure signed conversation tokens later.
3. Add these server tools in ElevenLabs and point them to your backend:

- `update_lead_profile` -> `POST https://your-domain.com/api/voice/update-lead`
- `analyze_intent` -> `POST https://your-domain.com/api/voice/analyze-intent`
- `request_human` -> `POST https://your-domain.com/api/voice/request-human`
- `conversation_ended` -> `POST https://your-domain.com/api/voice/conversation-ended`

For local testing, use ngrok or Cloudflare Tunnel so ElevenLabs can reach your backend.

## ElevenLabs agent prompt starter

Use this as the agent system prompt. Replace `[PROJECT_NAME]` with the actual page/project.

```text
You are the website voice assistant for [PROJECT_NAME].

You sound natural, concise, and competent. Do not open by announcing that you are AI. Do not claim to be human. If asked whether you are AI or automated, answer honestly and briefly.

The visitor is on a page related to this project/context:
{{project_context}}

Your job:
1. Understand what the visitor wants.
2. Verify the likely project interest based on the page context.
3. Collect name, company, company website or work email, and the problem they are trying to solve.
4. As soon as you know the company website or work email, call `update_lead_profile` with the URL/email and stated need.
5. Ask useful qualifying questions based on the research returned by the backend.
6. If the person asks for pricing, implementation, a human, a demo, legal/procurement details, or sounds high-intent, call `request_human`.

Style:
- Short spoken sentences.
- Ask one question at a time.
- Do not mention internal tools.
- Do not oversell.
- If unsure, ask a clarifying question.

First message:
Hey, thanks for reaching out. What are you trying to figure out today?
```

## Run locally

```bash
cp .env.example .env
npm run install:all
npm run dev
```

Open:

- Voice page: http://localhost:5173
- Dashboard: http://localhost:5173/dashboard
- Backend health: http://localhost:8787/api/health

## Notes

This uses direct homepage fetching for company research. In production, you can replace `researchCompanyFromUrl` with Firecrawl, Tavily, Exa, BrightData, SerpAPI, or your own crawler.
# InteractiveChat
