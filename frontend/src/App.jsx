import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { fetchBrand, sendChat, resetSession } from './api.js'

// ---- helpers --------------------------------------------------------------

function newSessionId() {
  if (crypto.randomUUID) return crypto.randomUUID()
  return 'sess-' + Math.random().toString(36).slice(2) + Date.now().toString(36)
}

// pick readable text color (black/white) for a given hex background
function contrastText(hex) {
  try {
    let h = hex.replace('#', '')
    if (h.length === 3) h = h.split('').map((c) => c + c).join('')
    const r = parseInt(h.slice(0, 2), 16)
    const g = parseInt(h.slice(2, 4), 16)
    const b = parseInt(h.slice(4, 6), 16)
    const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return lum > 0.6 ? '#111111' : '#ffffff'
  } catch {
    return '#ffffff'
  }
}

function injectFonts(fonts) {
  const families = [...new Set(fonts.filter(Boolean))]
    .map((f) => f.split(',')[0].trim().replace(/['"]/g, ''))
    .filter((f) => f && !/^(system-ui|sans-serif|serif|-apple-system)/i.test(f))
    .map((f) => `family=${encodeURIComponent(f)}:wght@400;500;600;700`)
  if (!families.length) return
  const id = 'brand-fonts'
  if (document.getElementById(id)) return
  const link = document.createElement('link')
  link.id = id
  link.rel = 'stylesheet'
  link.href = `https://fonts.googleapis.com/css2?${families.join('&')}&display=swap`
  document.head.appendChild(link)
}

// open links in a new tab, and render inline [n] markers as "source page" links
const mdComponents = {
  a: (props) => <a target="_blank" rel="noopener noreferrer" {...props} />,
}

// Strip inline [n] reference markers from the answer text — sources are shown
// as "Source page" button(s) beneath the answer instead of cluttering each line.
// (The backend still requires a citation before showing any answer, so grounding
// is unaffected; we just don't render the inline markers.)
function stripCitations(text) {
  if (!text) return text
  return text
    .replace(/\s*\[\d+\]/g, '')     // drop " [1]" markers (and any leading space)
    .replace(/[ \t]{2,}/g, ' ')     // tidy any doubled spaces left behind
    .replace(/\(\s*\)/g, '')        // drop empty parens if the model wrote "(...)"
}

// ---- citations ------------------------------------------------------------

function Citations({ citations }) {
  const [open, setOpen] = useState(null)
  if (!citations || citations.length === 0) return null
  const single = citations.length === 1
  return (
    <div className="citations">
      <div className="chips">
        {citations.map((c, i) => (
          <button
            key={c.n}
            className={`chip ${open === c.n ? 'chip-active' : ''}`}
            onClick={() => setOpen(open === c.n ? null : c.n)}
            title={c.page_title}
          >
            Source page{single ? '' : ` ${i + 1}`}
          </button>
        ))}
      </div>
      {open != null && (() => {
        const c = citations.find((x) => x.n === open)
        if (!c) return null
        return (
          <div className="cite-card">
            <div className="cite-title">{c.page_title || 'Source'}</div>
            {c.heading_path && <div className="cite-path">{c.heading_path}</div>}
            {c.snippet && <div className="cite-snippet">“{c.snippet}…”</div>}
            <a className="cite-link" href={c.source_url} target="_blank" rel="noopener noreferrer">
              Open source page ↗
            </a>
          </div>
        )
      })()}
    </div>
  )
}

// ---- message bubble -------------------------------------------------------

function Message({ msg }) {
  const isUser = msg.role === 'user'
  return (
    <div className={`row ${isUser ? 'row-user' : 'row-bot'}`}>
      <div className={`bubble ${isUser ? 'bubble-user' : msg.error ? 'bubble-error' : 'bubble-bot'}`}>
        {isUser ? (
          <span>{msg.content}</span>
        ) : (
          <div className="markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
              {msg.error ? msg.content : stripCitations(msg.content)}
            </ReactMarkdown>
          </div>
        )}
        {!isUser && !msg.error && <Citations citations={msg.citations} />}
      </div>
    </div>
  )
}

// ---- app ------------------------------------------------------------------

const GREETING = {
  role: 'assistant',
  content:
    "Hi! I'm the Cameron County website assistant. Ask me about county departments, "
    + 'services, offices, hours, or documents — I answer using only content from '
    + 'cameroncountytx.gov and cite my sources.',
  citations: [],
  grounded: true,
}

export default function App() {
  const [brand, setBrand] = useState(null)
  const [messages, setMessages] = useState([GREETING])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const sessionRef = useRef(newSessionId())
  const scrollRef = useRef(null)

  // load + apply brand tokens
  useEffect(() => {
    fetchBrand()
      .then((b) => {
        setBrand(b)
        const root = document.documentElement.style
        const primary = b.primary || '#1f4e79'
        root.setProperty('--brand-primary', primary)
        root.setProperty('--brand-primary-text', contrastText(primary))
        root.setProperty('--brand-secondary', b.secondary || '#2c3e50')
        root.setProperty('--brand-accent', b.accent || '#00bcd4')
        root.setProperty('--brand-font-body', b.font_body ? `'${b.font_body}', system-ui, sans-serif` : 'system-ui, sans-serif')
        root.setProperty('--brand-font-heading', b.font_heading ? `'${b.font_heading}', system-ui, sans-serif` : 'system-ui, sans-serif')
        injectFonts([b.font_body, b.font_heading])
      })
      .catch(() => setBrand({ fallbacks: ['all'] }))
  }, [])

  // autoscroll
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [messages, loading])

  async function send() {
    const q = input.trim()
    if (!q || loading) return
    setInput('')
    setMessages((m) => [...m, { role: 'user', content: q }])
    setLoading(true)
    try {
      const res = await sendChat(q, sessionRef.current)
      setMessages((m) => [...m, {
        role: 'assistant',
        content: res.answer,
        citations: res.citations || [],
        grounded: res.grounded,
      }])
    } catch (err) {
      setMessages((m) => [...m, {
        role: 'assistant',
        content: `⚠️ ${err.message || 'Something went wrong. Please try again.'}`,
        error: true,
      }])
    } finally {
      setLoading(false)
    }
  }

  function onKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  async function newConversation() {
    await resetSession(sessionRef.current)
    sessionRef.current = newSessionId()
    setMessages([GREETING])
    setInput('')
  }

  const logoSrc = brand?.logo_path || null
  const usingFallback = brand?.fallbacks && brand.fallbacks.length > 0

  return (
    <div className="app">
      <header className="header">
        <div className="header-left">
          {logoSrc
            ? <img className="logo" src={logoSrc} alt="Cameron County logo" />
            : <div className="logo-fallback">CC</div>}
          <div className="header-titles">
            <div className="title">Cameron County</div>
            <div className="subtitle">Website Assistant</div>
          </div>
        </div>
        <button className="newchat" onClick={newConversation}>New conversation</button>
      </header>

      <main className="messages" ref={scrollRef}>
        {messages.map((m, i) => <Message key={i} msg={m} />)}
        {loading && (
          <div className="row row-bot">
            <div className="bubble bubble-bot typing">
              <span className="dot" /><span className="dot" /><span className="dot" />
            </div>
          </div>
        )}
      </main>

      <footer className="composer">
        <textarea
          className="input"
          rows={1}
          placeholder="Ask about county services, offices, hours, documents…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKey}
          maxLength={1000}
        />
        <button className="send" onClick={send} disabled={loading || !input.trim()}>
          {loading ? '…' : 'Send'}
        </button>
      </footer>

      <div className="foot-note">
        Answers come only from cameroncountytx.gov. Always verify critical details with the county.
        {usingFallback && ' · (Using a neutral theme — brand tokens unavailable.)'}
      </div>
    </div>
  )
}
