// Thin API client for the FastAPI backend.

export async function fetchBrand() {
  const r = await fetch('/brand')
  if (!r.ok) throw new Error('brand fetch failed')
  return r.json()
}

export async function sendChat(question, sessionId) {
  const r = await fetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, session_id: sessionId }),
  })
  if (!r.ok) {
    let detail = `Request failed (${r.status})`
    try {
      const body = await r.json()
      if (body && body.detail) detail = body.detail
    } catch { /* ignore */ }
    throw new Error(detail)
  }
  return r.json()
}

export async function resetSession(sessionId) {
  try {
    await fetch('/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    })
  } catch { /* best-effort */ }
}
