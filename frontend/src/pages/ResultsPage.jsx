import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import client from '../api/client'

const SEV_CLS  = { P0: 'sev-p0', P1: 'sev-p1', P2: 'sev-p2', P3: 'sev-p3' }
const SRC_CLS  = { github: 'sb-gh', jira_apache: 'sb-jira', bugzilla: 'sb-bz', confluence: 'sb-cf' }
const SRC_LBL  = { github: 'GH', jira_apache: 'JIRA', bugzilla: 'BZ', confluence: 'CF' }

const TEAM_COLORS = [
  { bg: 'var(--blue-lt)',   color: 'var(--blue)',   bd: 'var(--blue-bd)'   },
  { bg: 'var(--purple-lt)', color: 'var(--purple)', bd: 'var(--purple-bd)' },
  { bg: 'var(--teal-lt)',   color: 'var(--teal)',   bd: 'var(--teal-bd)'   },
  { bg: 'var(--amber-lt)',  color: 'var(--amber)',  bd: 'var(--amber-bd)'  },
]

function SevBadge({ sev }) {
  return <span className={`sev ${SEV_CLS[sev] || 'sev-unk'}`}>{sev || 'UNK'}</span>
}
function SrcBadge({ type }) {
  const cls = SRC_CLS[type] || 'sb-jira'
  const lbl = SRC_LBL[type] || (type || '?').toUpperCase().slice(0, 4)
  return <span className={`sb ${cls}`}>{lbl}</span>
}

const toPercent = (score) => {
  if (score == null) return 0
  if (score > 1) return Math.min(Math.round(score), 100)
  return Math.min(Math.round(score * 100), 100)
}

function TicketLink({ ticket }) {
  const isValidUrl = ticket.url && ticket.url.startsWith('https://')
  const ticketId   = ticket.id || ticket.ticket_id
  if (isValidUrl) {
    return (
      <a
        href={ticket.url}
        target="_blank"
        rel="noopener noreferrer"
        style={{ color: 'inherit', textDecoration: 'underline', cursor: 'pointer' }}
        onClick={(e) => e.stopPropagation()}
      >
        {ticketId}
      </a>
    )
  }
  return <span>{ticketId}</span>
}

export default function ResultsPage() {
  const { caseId }  = useParams()
  const navigate    = useNavigate()
  const [result,    setResult]  = useState(null)
  const [loading,   setLoading] = useState(true)

  useEffect(() => {
    client.get(`/triage/${caseId}/result`)
      .then((r) => setResult(r.data))
      .catch(() => setResult(null))
      .finally(() => setLoading(false))
  }, [caseId])

  if (loading) {
    return (
      <div style={{ padding: '60px 0', textAlign: 'center', color: 'var(--text3)', fontFamily: 'JetBrains Mono, monospace', fontSize: 13 }}>
        Loading results…
      </div>
    )
  }

  if (!result) {
    return (
      <div style={{ padding: '60px 0', textAlign: 'center' }}>
        <p style={{ color: 'var(--text3)', marginBottom: 16, fontSize: 14 }}>
          Results not found for case <span className="mono">{caseId}</span>
        </p>
        <button className="btn btn-teal" onClick={() => navigate('/bugs')}>Back to Bugs</button>
      </div>
    )
  }

  const ctx        = result.context || {}
  const synthesis  = ctx.synthesis  || {}
  const ticket     = ctx.primary_ticket || {}
  const conf       = toPercent(synthesis.confidence)
  const sevBlockCls = { P0: 'p0-b', P1: 'p1-b', P2: 'p2-b', P3: 'p3-b' }[synthesis.unified_severity] || 'p3-b'
  const caseShort  = `BT-${caseId.slice(-5).toUpperCase()}`
  const srcType    = ticket.system_type || ticket.source

  return (
    <div className="fade-in">
      {/* Top bar */}
      <div className="result-topbar">
        <button className="btn btn-ghost btn-sm" onClick={() => navigate('/bugs')}>← Back to Bugs</button>
        <span className="bt-badge">{caseShort}</span>
        <span className="mono" style={{ fontSize: 12, color: 'var(--blue)' }}>{result.bug_id}</span>
        <h2>{ticket.title || result.bug_id}</h2>
        <div className="result-topbar-right">
          {srcType && <SrcBadge type={srcType} />}
        </div>
      </div>

      {/* 2×2 grid */}
      <div className="result-grid">

        {/* Panel 1: Bug Context */}
        <div className="panel teal-t">
          <div className="panel-hdr">
            <div className="panel-num pn-teal">01</div>
            <span className="panel-title">Bug Context</span>
            {srcType && <SrcBadge type={srcType} />}
          </div>
          <div className="panel-body scroll">
            {ticket.status    && <div className="meta-row"><span className="meta-k">Status</span>   <span className="meta-v">{ticket.status}</span></div>}
            {ticket.severity  && <div className="meta-row"><span className="meta-k">Severity</span> <SevBadge sev={ticket.severity} /></div>}
            {ticket.component && <div className="meta-row"><span className="meta-k">Component</span><span className="meta-v">{ticket.component}</span></div>}
            {ticket.assignee  && <div className="meta-row"><span className="meta-k">Assignee</span> <span className="meta-v">{ticket.assignee}</span></div>}
            {ticket.reporter  && <div className="meta-row"><span className="meta-k">Reporter</span> <span className="meta-v">{ticket.reporter}</span></div>}
            {ticket.description && (
              <>
                <div className="panel-div" />
                <p className="desc-txt">{ticket.description.slice(0, 500)}{ticket.description.length > 500 ? '…' : ''}</p>
              </>
            )}
          </div>
        </div>

        {/* Panel 2: Related Issues */}
        <div className="panel blue-t">
          <div className="panel-hdr">
            <div className="panel-num pn-blue">02</div>
            <span className="panel-title">Related Issues</span>
            <span className="panel-badge pb-blue">{ctx.related_tickets?.length || 0} found</span>
          </div>
          <div className="panel-body scroll">
            {!ctx.related_tickets?.length ? (
              <p style={{ color: 'var(--text3)', fontSize: 13 }}>No related issues found.</p>
            ) : ctx.related_tickets.map((t, i) => {
              const score    = t.similarity_score || t.relevance_score || 0
              const pct      = toPercent(score)
              const simCls   = score >= 0.8 ? 'h' : score >= 0.6 ? 'm' : 'l'
              const fillCls  = score >= 0.8 ? 'sim-h' : score >= 0.6 ? 'sim-m' : 'sim-l'
              const barColor = score >= 0.8 ? 'var(--teal)' : 'var(--orange)'
              const hasUrl   = t.url && t.url.startsWith('https://')
              return (
                <div key={i} className="issue-card">
                  <div className="issue-top">
                    <span className="mono" style={{ fontSize: 11, color: hasUrl ? 'var(--blue)' : 'var(--text2)', flexShrink: 0 }}>
                      <TicketLink ticket={t} />
                    </span>
                    <span className="issue-name">{t.title?.slice(0, 60)}</span>
                    {t.severity && <SevBadge sev={t.severity} />}
                    {hasUrl && (
                      <a href={t.url} target="_blank" rel="noopener noreferrer" className="ext-btn" onClick={(e) => e.stopPropagation()}>↗</a>
                    )}
                  </div>
                  <div className="sim-row">
                    <div className="sim-bar">
                      <div className={`sim-fill ${fillCls}`} style={{ width: `${pct}%`, background: barColor }} />
                    </div>
                    <span className={`sim-pct ${simCls}`}>{pct}%</span>
                  </div>
                  {t.similarity_reason && (
                    <p className="sim-reason">{t.similarity_reason}</p>
                  )}
                </div>
              )
            })}
          </div>
        </div>

        {/* Panel 3: Knowledge Base (placeholder for results view) */}
        <div className="panel amber-t">
          <div className="panel-hdr">
            <div className="panel-num pn-amber">03</div>
            <span className="panel-title">Knowledge Base</span>
          </div>
          <div className="panel-body">
            {ctx.kb_articles?.length > 0 ? (
              ctx.kb_articles.map((a, i) => (
                <div key={i} className="kb-card">
                  <div className="kb-icon">KB</div>
                  <div className="kb-info">
                    <strong>{a.title}</strong>
                    <small>{a.space || ''}{a.relevance ? ` · ${a.relevance}` : ''}</small>
                  </div>
                </div>
              ))
            ) : (
              <p style={{ color: 'var(--text3)', fontSize: 13 }}>No KB articles linked.</p>
            )}
          </div>
        </div>

        {/* Panel 4: AI Summary */}
        <div className="panel purple-t">
          <div className="panel-hdr">
            <div className="panel-num pn-purple">04</div>
            <span className="panel-title">AI Summary</span>
            {conf > 0 && <span className="panel-badge pb-purple">{conf.toFixed(0)}% confidence</span>}
          </div>
          <div className="panel-body scroll">
            {synthesis.used_fallback && (
              <div className="fallback-warn">⚠ Fallback analysis used — AI synthesis was unavailable</div>
            )}
            {synthesis.unified_severity && (
              <div className={`sev-block ${sevBlockCls}`}>
                <div className="sev-big">{synthesis.unified_severity}</div>
                {synthesis.severity_rationale && <div className="sev-reason">{synthesis.severity_rationale}</div>}
              </div>
            )}
            {conf > 0 && (
              <div className="conf-row">
                <span className="conf-num">{conf.toFixed(0)}%</span>
                <div className="conf-bar-wrap">
                  <div className="conf-bar"><div className="conf-fill" style={{ width: `${conf}%` }} /></div>
                  <div className="conf-label">AI confidence score</div>
                </div>
              </div>
            )}
            {synthesis.root_cause && (
              <>
                <div className="sec-label">Root Cause</div>
                <div className="root-box"><p>{synthesis.root_cause}</p></div>
              </>
            )}
            {synthesis.recommended_actions?.length > 0 && (
              <>
                <div className="sec-label">Recommended Actions</div>
                <ol className="rec-list">
                  {synthesis.recommended_actions.map((a, i) => (
                    <li key={i}><span className="rec-num">{String(i + 1).padStart(2, '0')}.</span>{a}</li>
                  ))}
                </ol>
              </>
            )}
            {synthesis.affected_teams?.length > 0 && (
              <>
                <div className="sec-label">Affected Teams</div>
                <div className="teams-wrap">
                  {synthesis.affected_teams.map((t, i) => {
                    const tc = TEAM_COLORS[i % TEAM_COLORS.length]
                    return (
                      <span key={i} className="team-tag" style={{ background: tc.bg, color: tc.color, border: `1px solid ${tc.bd}` }}>{t}</span>
                    )
                  })}
                </div>
              </>
            )}
            {(synthesis.engineer_summary || synthesis.customer_summary) && (
              <div className="summaries-grid">
                {synthesis.engineer_summary && (
                  <div className="summary-card">
                    <div className="summary-card-lbl">Engineer Summary</div>
                    <p className="summary-card-txt">{synthesis.engineer_summary}</p>
                  </div>
                )}
                {synthesis.customer_summary && (
                  <div className="summary-card">
                    <div className="summary-card-lbl">Customer Summary</div>
                    <p className="summary-card-txt">{synthesis.customer_summary}</p>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

      </div>
    </div>
  )
}
