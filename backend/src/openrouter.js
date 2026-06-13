const OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions';

export async function openRouterJson({ model, system, user, schema, temperature = 0.2 }) {
  if (!process.env.OPENROUTER_API_KEY) {
    throw new Error('OPENROUTER_API_KEY is missing');
  }

  const response = await fetch(OPENROUTER_URL, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${process.env.OPENROUTER_API_KEY}`,
      'Content-Type': 'application/json',
      'HTTP-Referer': process.env.OPENROUTER_SITE_URL || 'http://localhost:5173',
      'X-Title': process.env.OPENROUTER_SITE_NAME || 'Website Voice Agent Starter'
    },
    body: JSON.stringify({
      model,
      messages: [
        { role: 'system', content: system },
        { role: 'user', content: user }
      ],
      temperature,
      response_format: schema
        ? {
            type: 'json_schema',
            json_schema: {
              name: schema.name,
              strict: true,
              schema: schema.schema
            }
          }
        : { type: 'json_object' }
    })
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`OpenRouter error ${response.status}: ${text}`);
  }

  const data = await response.json();
  const content = data.choices?.[0]?.message?.content;
  if (!content) throw new Error('OpenRouter returned no content');

  try {
    return JSON.parse(content);
  } catch (err) {
    throw new Error(`Could not parse OpenRouter JSON: ${content}`);
  }
}
