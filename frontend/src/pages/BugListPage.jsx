import { useState, useEffect, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { getBugs, getBugStatus, refreshBugCache, getMetrics } from '../api/bugs'
import { startTriage } from '../api/triage'

const toPercent = (score) => {
  if (score == null) return 0
  if (score > 1) return Math.min(Math.round(score), 100)
  return Math.min(Math.round(score * 100), 100)
}

const SRC_CLS = { github: 'sb-gh', jira_apache: 'sb-jira', bugzilla: 'sb-bz', confluence: 'sb-cf', customer_portal: 'sb-jira' }
const SRC_LBL = { github: 'GH', jira_apache: 'JIRA', bugzilla: 'BZ', confluence: 'CF', customer_portal: 'CP' }
const SEV_CLS = { P0: 'sev-p0', P1: 'sev-p1', P2: 'sev-p2', P3: 'sev-p3' }
const SEVERITY_ORDER = ['P0', 'P1', 'P2', 'P3', 'Unknown']
const ALL_SOURCES = ['All Sources', 'github', 'jira_apache', 'bugzilla']

function SevBadge({ sev }) {
  return <span className={`sev ${SEV_CLS[sev] || 'sev-unk'}`}>{sev || 'UNK'}</span>
}

function SrcBadge({ type }) {
  return <span className={`sb ${SRC_CLS[type] || 'sb-jira'}`}>{SRC_LBL[type] || (type || '?').toUpperCase().slice(0, 4)}</span>
}

function fmtDate(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
  } catch { return '—' }
}

/* ─── Status panel (SD7 / SD8 / SD9) shown when expanding an untriaged row ─── */
function formatChange(change) {
  if (!change) return ''
  if (typeof change === 'string') return change
  const field = change.field || 'field'
  const from = change.from || 'empty'
  const to = change.to || 'empty'
  return `${field}: ${from} -> ${to}`
}

function BugStatusPanel({ bugId, status, loading, onTriage, onView, triaging }) {
  if (loading) {
    return (
      <div style={{
        background: 'var(--bg)', borderTop: '1px solid var(--border)',
        padding: '12px 24px', display: 'flex', flexDirection: 'column', gap: 6,
      }}>
        <div className="skeleton-pulse" style={{ width: 180, height: 13, borderRadius: 3 }} />
        <div className="skeleton-pulse" style={{ width: 120, height: 13, borderRadius: 3 }} />
      </div>
    )
  }
  if (!status) return null

  // SD9 — never triaged
  if (status.is_new) {
    return (
      <div style={{
        background: 'var(--bg)', borderTop: '1px solid var(--border)',
        padding: '12px 24px', display: 'flex', alignItems: 'center', gap: 14,
      }}>
        <span style={{ fontSize: 13, color: 'var(--text3)' }}>Never triaged</span>
        <button className="btn btn-teal btn-sm" onClick={() => onTriage(bugId)} disabled={triaging === bugId}>
          {triaging === bugId ? '…' : '▶ Triage'}
        </button>
      </div>
    )
  }

  const lastDate = fmtDate(status.last_triaged_at)
  const confPct  = status.last_confidence != null ? toPercent(status.last_confidence) : null

  // SD7 — changes found
  if (status.needs_retriage && status.changes?.length > 0) {
    return (
      <div style={{ background: 'var(--bg)', borderTop: '1px solid var(--border)', padding: '12px 24px' }}>
        <div style={{
          background: 'var(--orange-lt)', border: '1px solid var(--orange-bd)',
          borderRadius: 7, padding: '9px 12px', marginBottom: 10, fontSize: 12, color: 'var(--orange)',
        }}>
          <div style={{ fontWeight: 700, marginBottom: 4 }}>⚠ Changes detected since last triage:</div>
          {status.changes.map((c, i) => <div key={i} style={{ marginLeft: 8 }}>• {c}</div>)}
        </div>
        <div style={{ fontSize: 11, color: 'var(--text3)', fontFamily: 'JetBrains Mono, monospace', marginBottom: 10 }}>
          Last triaged: {lastDate}
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {status.case_id && (
            <button className="btn btn-outline btn-sm" onClick={() => onView(status.case_id)}>View Previous Results</button>
          )}
          <button className="btn btn-teal btn-sm" onClick={() => onTriage(bugId)} disabled={triaging === bugId}>
            {triaging === bugId ? '…' : '▶ Run Fresh Triage'}
          </button>
        </div>
      </div>
    )
  }

  // SD8 — no changes
  return (
    <div style={{ background: 'var(--bg)', borderTop: '1px solid var(--border)', padding: '12px 24px' }}>
      <div style={{ fontSize: 13, color: 'var(--green)', fontWeight: 600, marginBottom: 5 }}>
        ✓ No changes since last triage
      </div>
      <div style={{ fontSize: 11, color: 'var(--text3)', fontFamily: 'JetBrains Mono, monospace', marginBottom: 10 }}>
        Last triaged: {lastDate}{confPct != null ? ` · Confidence: ${confPct}%` : ''}
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        {status.case_id && (
          <button className="btn btn-outline btn-sm" onClick={() => onView(status.case_id)}>View Previous Results</button>
        )}
        <button className="btn btn-ghost btn-sm" onClick={() => onTriage(bugId, true)} disabled={triaging === bugId}>
          {triaging === bugId ? '…' : 'Re-triage Anyway'}
        </button>
      </div>
    </div>
  )
}

/* ─── Expandable flat row for UNTRIAGED bugs ─── */
function ExpandableBugRow({ bug, onTriage, triaging, navigate }) {
  const [expanded,      setExpanded]      = useState(false)
  const [status,        setStatus]        = useState(null)
  const [statusLoading, setStatusLoading] = useState(false)

  const handleExpand = async () => {
    const next = !expanded
    setExpanded(next)
    if (next && !status) {
      setStatusLoading(true)
      try {
        const s = await getBugStatus(bug.ticket_id)
        setStatus({
          ...s,
          changes: (s.changes || []).map(formatChange),
        })
      } catch {
        setStatus({ is_new: true, needs_retriage: true, changes: [] })
      } finally {
        setStatusLoading(false)
      }
    }
  }

  const handleView = (caseId) => navigate(`/triage/${caseId}?from=history`)

  return (
    <div style={{
      background: 'var(--white)', border: '1px solid var(--border)',
      borderRadius: 8, marginBottom: 5, overflow: 'hidden',
    }}>
      <div className="bug-flat" style={{ borderRadius: 0, border: 'none', marginBottom: 0 }}>
        <button onClick={handleExpand} style={{
          background: 'none', border: 'none', cursor: 'pointer',
          fontSize: 10, color: 'var(--text3)', padding: '2px 4px', flexShrink: 0,
        }}>
          {expanded ? '▼' : '▶'}
        </button>
        <SrcBadge type={bug.system_type} />
        <span className="raw-id">{bug.ticket_id}</span>
        <span className="bug-flat-title">{bug.title}</span>
        <SevBadge sev={bug.severity} />
        <span className="bug-status-pill">{bug.status || 'open'}</span>
        <span className="bug-flat-time">
          {bug.updated_at ? new Date(bug.updated_at).toLocaleDateString() : '—'}
        </span>
        <button
          className="btn btn-teal btn-sm"
          onClick={() => onTriage(bug)}
          disabled={triaging === bug.ticket_id}
        >
          {triaging === bug.ticket_id ? '…' : '▶ Triage'}
        </button>
      </div>
      {expanded && (
        <BugStatusPanel
          bugId={bug.ticket_id}
          status={status}
          loading={statusLoading}
          onTriage={() => onTriage(bug)}
          onView={handleView}
          triaging={triaging}
        />
      )}
    </div>
  )
}

/* ─── Tree row for TRIAGED bugs — shows AI analysis as child ─── */
function TriagedBugRow({ bug, onRetriage, retriaging, navigate }) {
  const [open, setOpen] = useState(false)
  const triage = bug.triage_info || {}
  const confPct = triage.confidence != null ? toPercent(triage.confidence) : null
  const triagedAt = fmtDate(triage.triaged_at)
  const caseIdShort = triage.case_id ? `BT-${triage.case_id.slice(0, 6).toUpperCase()}` : 'BT-?'
  const systems = triage.systems_queried || []

  return (
    <div style={{
      background: 'var(--white)', borderRadius: 8, marginBottom: 5, overflow: 'hidden',
      border: '1px solid var(--border)', borderLeft: '4px solid var(--teal)',
    }}>
      {/* Root row */}
      <div
        className="bug-flat"
        style={{ borderRadius: 0, border: 'none', marginBottom: 0, cursor: 'pointer' }}
        onClick={() => setOpen((v) => !v)}
      >
        <span style={{
          fontSize: 10, color: 'var(--text3)', padding: '2px 4px', flexShrink: 0,
          transform: open ? 'rotate(90deg)' : 'none', display: 'inline-block', transition: 'transform 0.15s',
        }}>▶</span>
        <span className="bt-badge">{caseIdShort}</span>
        <SrcBadge type={bug.system_type} />
        <span className="raw-id">{bug.ticket_id}</span>
        <span className="bug-flat-title">{bug.title}</span>
        <SevBadge sev={triage.severity || bug.severity} />
        {confPct != null && (
          <span className="match-badge match-h">{confPct}%</span>
        )}
        <span className="current-badge">✓ Current</span>
        <span className="bug-flat-time">{triagedAt}</span>
        <button
          className="btn btn-outline btn-sm"
          onClick={(e) => { e.stopPropagation(); onRetriage(bug, true) }}
          disabled={retriaging === bug.ticket_id}
        >
          {retriaging === bug.ticket_id ? '…' : 'Re-triage'}
        </button>
      </div>

      {/* Expanded children — AI analysis */}
      {open && (
        <div style={{ borderTop: '1px solid var(--border)', background: 'var(--bg)' }}>
          <div style={{
            marginLeft: 24, paddingLeft: 16, paddingTop: 10, paddingBottom: 10,
            borderLeft: '2px solid var(--border)', position: 'relative',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 6 }}>
              <span style={{ fontSize: 10, color: 'var(--text3)', fontFamily: 'JetBrains Mono, monospace' }}>└─</span>
              <span style={{ fontSize: 12, color: 'var(--teal)', fontWeight: 700 }}>AI Analysis</span>
              {triage.severity && <SevBadge sev={triage.severity} />}
              {triage.case_id && (
                <button
                  className="btn btn-ghost btn-sm"
                  style={{ fontSize: 11 }}
                  onClick={(e) => { e.stopPropagation(); navigate(`/triage/${triage.case_id}?from=history`) }}
                >
                  View Results ↗
                </button>
              )}
            </div>
            {systems.length > 0 && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11, color: 'var(--text3)', marginLeft: 24 }}>
                <span style={{ fontFamily: 'JetBrains Mono, monospace' }}>└─</span>
                <span>Systems checked: <strong>{systems.join(', ')}</strong></span>
                {confPct != null && (
                  <span style={{ color: 'var(--teal)', marginLeft: 8 }}>Confidence: {confPct}%</span>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

export default function BugListPage() {
  const [bugs,          setBugs]          = useState([])
  const [total,         setTotal]         = useState(0)
  const [page,          setPage]          = useState(1)
  const [loading,       setLoading]       = useState(true)
  const [searchInput,   setSearchInput]   = useState('')
  const [search,        setSearch]        = useState('')
  const [severity,      setSeverity]      = useState('')
  const [source,        setSource]        = useState('')
  const [status,        setStatus]        = useState('open')
  const [activePill,    setActivePill]    = useState('All')
  const [triagingId,    setTriagingId]    = useState(null)
  const [lastSynced,    setLastSynced]    = useState(null)
  const [directBugId,   setDirectBugId]  = useState('')
  const [refreshing,    setRefreshing]    = useState(false)
  const [sourcesOnline, setSourcesOnline] = useState(0)
  const [isPartial,     setIsPartial]     = useState(false)
  const [cacheStatus,   setCacheStatus]   = useState(null)
  const [metrics,       setMetrics]       = useState(null)
  const navigate      = useNavigate()
  const intervalRef   = useRef(null)
  const pollCountRef  = useRef(0)

  // Debounce search
  useEffect(() => {
    const timer = setTimeout(() => { setSearch(searchInput); setPage(1) }, 500)
    return () => clearTimeout(timer)
  }, [searchInput])

  const fetchBugs = useCallback(async (silent = false) => {
    if (!silent) {
      setLoading(true)
      pollCountRef.current = 0
    }
    try {
      const data = await getBugs({ page, severity, source: source || undefined, status, search })
      // Backend returns a flat `bugs` array (ungrouped + group children).
      // Fall back to `ungrouped` for older cached responses that predate this field.
      const allBugs = data.bugs || [...(data.ungrouped || []), ...(data.groups || [])]
      setBugs(allBugs)
      setTotal(data.total || 0)
      setSourcesOnline(data.sources_online || 0)
      setIsPartial(data.partial || false)
      setLastSynced(new Date())
      setCacheStatus(data.cache_status || 'hit')
    } catch (e) {
      console.error('Failed to fetch bugs', e)
    } finally {
      if (!silent) setLoading(false)
    }
  }, [page, severity, source, status, search])

  useEffect(() => {
    fetchBugs()
    intervalRef.current = setInterval(fetchBugs, 120000)
    return () => clearInterval(intervalRef.current)
  }, [fetchBugs])

  // Poll every 1 s on cold start until data arrives (max 3 polls, then stop)
  useEffect(() => {
    if (cacheStatus !== 'cold') return
    if (pollCountRef.current >= 3) return  // hard stop at 3

    const timer = setTimeout(() => {
      pollCountRef.current += 1
      fetchBugs(true)  // silent fetch
    }, 1000)

    return () => clearTimeout(timer)
  }, [cacheStatus, fetchBugs])

  // Fetch metrics for dashboard strip
  useEffect(() => {
    getMetrics().then(setMetrics).catch(console.error)
  }, [])

  const handleTriage = async (bugOrId, forceRefresh = false) => {
    // Accept either a full bug object { ticket_id, source_id } or a plain string (direct triage bar)
    const bugId    = typeof bugOrId === 'string' ? bugOrId : bugOrId.ticket_id
    const sourceId = typeof bugOrId === 'string' ? '' : (bugOrId.source_id || '')
    setTriagingId(bugId)
    try {
      const data = await startTriage(bugId, sourceId, forceRefresh)
      navigate(`/triage/${data.case_id}`)
    } catch (e) {
      alert('Failed to start triage: ' + (e.response?.data?.detail || e.message))
    } finally {
      setTriagingId(null)
    }
  }

  const handleRefresh = async () => {
    setRefreshing(true)
    try { await refreshBugCache() } catch { /* ignore */ }
    await new Promise((r) => setTimeout(r, 3000))
    await fetchBugs()
    setRefreshing(false)
  }

  const syncMinsAgo = lastSynced ? Math.round((Date.now() - lastSynced) / 60000) : null

  const visibleBugs = bugs.filter((b) => {
    if (activePill === 'All')       return true
    if (activePill === 'Untriaged') return !b.is_triaged
    if (activePill === 'Triaged')   return b.is_triaged
    if (activePill === 'Critical')  return b.severity === 'P0' || b.severity === 'P1'
    return true
  })

  const triaged  = bugs.filter((b) => b.is_triaged).length
  const awaiting = bugs.length - triaged
  const start    = (page - 1) * 50 + 1
  const end      = Math.min((page - 1) * 50 + bugs.length, total)

  const triagedBugs   = visibleBugs.filter((b) => b.is_triaged)
  const untriagedBugs = visibleBugs.filter((b) => !b.is_triaged)

  return (
    <div>
      {/* Dashboard strip */}
      {metrics && (
        <div style={{
          display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap',
          background: 'var(--bg)', border: '1px solid var(--border)',
          borderRadius: 8, padding: '8px 14px', alignItems: 'center', fontSize: 12, fontWeight: 500,
        }}>
          <span style={{ color: 'var(--red)', fontWeight: 700 }}>🔴 P0: {metrics.live_p0_count ?? 0}</span>
          <span style={{ color: 'var(--text3)' }}>·</span>
          <span style={{ color: '#D97706', fontWeight: 700 }}>🟠 P1: {metrics.live_p1_count ?? 0}</span>
          <span style={{ color: 'var(--text3)' }}>·</span>
          <span style={{ color: 'var(--green)' }}>✅ Triaged Today: {metrics.triaged_today ?? 0}</span>
          <span style={{ color: 'var(--text3)' }}>·</span>
          <span style={{ color: '#D97706' }}>⏳ Needs Triage: {metrics.needs_triage ?? 0}</span>
          <span style={{ color: 'var(--text3)' }}>·</span>
          <span style={{ color: 'var(--red)' }}>Failed: {metrics.failed_triages ?? 0}</span>
          <span style={{ color: 'var(--text3)' }}>Â·</span>
          <span style={{ color: 'var(--teal)', fontWeight: 700 }}>
            🟢 {metrics.sources_online ?? 0}/{metrics.sources_total ?? 0} Systems Online
          </span>
        </div>
      )}

      {/* Header */}
      <div className="page-hdr-row">
        <div className="page-hdr">
          <h1>Auto-Discovered Bugs</h1>
          <p>{total} bugs · fetched live · Redis cache 2 min TTL · nothing stored</p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, alignSelf: 'flex-start', paddingTop: 4 }}>
          {syncMinsAgo !== null && (
            <span style={{ fontSize: 12, color: 'var(--text3)', fontFamily: 'JetBrains Mono, monospace' }}>
              ↺ Synced {syncMinsAgo} min ago
            </span>
          )}
          <button
            className="btn btn-ghost btn-sm"
            onClick={handleRefresh}
            disabled={refreshing || loading}
            style={{ fontFamily: 'inherit' }}
          >
            {refreshing ? 'Refreshing…' : '↺ Refresh'}
          </button>
        </div>
      </div>

      {/* Filter bar */}
      <div className="filter-bar">
        <div className="search-wrap">
          <span className="search-icon">🔍</span>
          <input
            className="form-input search-input"
            placeholder="Search by ID, title, keyword..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
        </div>

        <select className="form-select filter-select" style={{ width: 'auto' }} onChange={() => setPage(1)}>
          <option>All Projects</option>
        </select>

        <select
          className="form-select filter-select"
          style={{ width: 'auto' }}
          value={source}
          onChange={(e) => { setSource(e.target.value === 'All Sources' ? '' : e.target.value); setPage(1) }}
        >
          {ALL_SOURCES.map((s) => <option key={s} value={s === 'All Sources' ? '' : s}>{s}</option>)}
        </select>

        <select
          className="form-select filter-select"
          style={{ width: 'auto' }}
          value={severity}
          onChange={(e) => { setSeverity(e.target.value); setPage(1) }}
        >
          <option value="">All Severities</option>
          {SEVERITY_ORDER.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>

        <select
          className="form-select filter-select"
          style={{ width: 'auto' }}
          value={status}
          onChange={(e) => { setStatus(e.target.value); setPage(1) }}
        >
          <option value="">All Statuses</option>
          <option value="open">Open</option>
          <option value="in progress">In Progress</option>
          <option value="resolved">Resolved</option>
          <option value="closed">Closed</option>
        </select>

        <div className="filter-pills">
          {['All', 'Untriaged', 'Triaged', 'Critical'].map((p) => (
            <button
              key={p}
              className={`pill${activePill === p ? ' active' : ''}`}
              onClick={() => setActivePill(p)}
            >
              {p}
            </button>
          ))}
        </div>
      </div>

      {/* Direct Triage bar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
        <input
          className="form-input"
          style={{ flex: 1, maxWidth: 320 }}
          placeholder="Enter bug ID to triage directly..."
          value={directBugId}
          onChange={(e) => setDirectBugId(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && directBugId.trim()) handleTriage(directBugId.trim())
          }}
        />
        <button
          className="btn btn-teal btn-sm"
          disabled={!directBugId.trim() || triagingId === directBugId.trim()}
          onClick={() => handleTriage(directBugId.trim())}
        >
          {triagingId === directBugId.trim() ? '…' : 'Triage'}
        </button>
      </div>

      {/* Legend bar */}
      <div className="card" style={{ padding: '10px 14px', marginBottom: 10 }}>
        <div className="legend-bar">
          <span className="legend-key">KEY:</span>
          <span className="bt-badge">BT-001</span>
          <span style={{ fontSize: 11, color: 'var(--text3)' }}>= AI triage session</span>
          <span className="current-badge">✓ Current</span>
          <span style={{ fontSize: 11, color: 'var(--text3)' }}>= triaged, no changes</span>
          <span className="match-badge match-h">90%</span>
          <span style={{ fontSize: 11, color: 'var(--text3)' }}>= AI confidence</span>
          <span className="raw-id">DISK-779</span>
          <span style={{ fontSize: 11, color: 'var(--text3)' }}>= untriaged bug ID</span>
        </div>
      </div>

      {/* Stats line */}
      {!loading && (
        <div className="stats-line">
          Showing {start}–{end} of {total} bugs · {triaged} triaged · {awaiting} untriaged · Sort: Severity
        </div>
      )}

      {/* Partial results banner */}
      {!loading && isPartial && bugs.length > 0 && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10,
          background: 'var(--orange-lt)', border: '1px solid var(--orange-bd)',
          borderRadius: 7, padding: '8px 14px', fontSize: 12, color: 'var(--orange)',
        }}>
          <span style={{ flex: 1 }}>⚠ Showing partial results — some sources are still loading</span>
          <button className="btn btn-ghost btn-sm" onClick={handleRefresh} disabled={refreshing}>
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      )}

      {/* Non-blocking cold-start banner (shown while background fetch runs) */}
      {!loading && cacheStatus === 'cold' && bugs.length === 0 && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10,
          background: 'var(--bg)', border: '1px solid var(--border)',
          borderRadius: 7, padding: '10px 14px', fontSize: 13, color: 'var(--text2)',
        }}>
          <span style={{
            display: 'inline-block', width: 14, height: 14, borderRadius: '50%',
            border: '2px solid var(--teal)', borderTopColor: 'transparent',
            animation: 'spin 0.8s linear infinite', flexShrink: 0,
          }} />
          <span>Fetching live data… (first load)</span>
        </div>
      )}

      {/* Bug rows */}
      {loading && bugs.length === 0 ? (
        <div>
          {[...Array(5)].map((_, i) => (
            <div key={i} style={{
              height: '64px',
              background: 'var(--color-background-secondary)',
              borderRadius: '8px',
              marginBottom: '8px',
              opacity: 1 - (i * 0.15),
              animation: 'pulse 1.5s ease-in-out infinite',
            }} />
          ))}
        </div>
      ) : visibleBugs.length === 0 ? (
        (() => {
          const hasFilters = !!(search || severity || source || activePill !== 'All')
          if (hasFilters) {
            return (
              <div className="card" style={{ textAlign: 'center', padding: '40px', color: 'var(--text3)', fontSize: 13 }}>
                <div style={{ marginBottom: 12 }}>No bugs match the current filter.</div>
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={() => {
                    setSearchInput(''); setSearch(''); setSeverity(''); setSource(''); setActivePill('All'); setPage(1)
                  }}
                >
                  Clear filters
                </button>
              </div>
            )
          }
          if (cacheStatus === 'cold') return null  // banner above is shown instead
          return (
            <div className="card" style={{ textAlign: 'center', padding: '40px', color: 'var(--text3)', fontSize: 13 }}>
              <div style={{ marginBottom: 12 }}>No bugs found. Try refreshing.</div>
              <button className="btn btn-ghost btn-sm" onClick={() => fetchBugs()}>Retry</button>
            </div>
          )
        })()
      ) : (
        <div>
          {/* Untriaged bugs first — flat expandable rows */}
          {untriagedBugs.map((bug, idx) => (
            <ExpandableBugRow
              key={`${bug.ticket_id}-${idx}`}
              bug={bug}
              onTriage={handleTriage}
              triaging={triagingId}
              navigate={navigate}
            />
          ))}

          {/* Triaged bugs — tree rows with AI analysis children */}
          {triagedBugs.map((bug, idx) => (
            <TriagedBugRow
              key={`triaged-${bug.ticket_id}-${idx}`}
              bug={bug}
              onRetriage={(b, fr) => handleTriage(b, fr)}
              retriaging={triagingId}
              navigate={navigate}
            />
          ))}
        </div>
      )}

      {/* Pagination */}
      {total > 50 && (
        <div style={{ display: 'flex', justifyContent: 'center', gap: 8, marginTop: 20 }}>
          <button className="btn btn-ghost btn-sm" onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page === 1}>Previous</button>
          <span style={{ padding: '5px 12px', fontSize: 13, color: 'var(--text2)', fontFamily: 'JetBrains Mono, monospace' }}>Page {page}</span>
          <button className="btn btn-ghost btn-sm" onClick={() => setPage((p) => p + 1)} disabled={bugs.length < 50}>Next</button>
        </div>
      )}
    </div>
  )
}
