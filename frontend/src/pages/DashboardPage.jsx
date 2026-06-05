import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { getMetrics } from '../api/bugs'

const SOURCE_ICON = { jira_apache: 'J', bugzilla: 'BZ', github: 'GH', confluence: 'CF', customer_portal: 'CP' }
const SOURCE_CLS  = { jira_apache: 'ci-jira', bugzilla: 'ci-bz', github: 'ci-gh', confluence: 'ci-cf' }
const SEV_CLS     = { P0: 'sev-p0', P1: 'sev-p1', P2: 'sev-p2', P3: 'sev-p3' }

function timeAgo(iso) {
  if (!iso) return '—'
  try {
    const diff = Date.now() - new Date(iso).getTime()
    const mins = Math.floor(diff / 60000)
    if (mins < 1) return 'just now'
    if (mins < 60) return `${mins}m ago`
    const hrs = Math.floor(mins / 60)
    if (hrs < 24) return `${hrs}h ago`
    return `${Math.floor(hrs / 24)}d ago`
  } catch { return '—' }
}

function StatCard({ label, value, color, topBorder, sub }) {
  return (
    <div className={`stat-card ${topBorder}`}>
      <div className={`stat-val ${color}`}>{value ?? '—'}</div>
      <div className="stat-label">{label}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  )
}

export default function DashboardPage() {
  const [metrics, setMetrics] = useState(null)
  const navigate = useNavigate()

  useEffect(() => {
    getMetrics().then(setMetrics).catch(console.error)
  }, [])

  const liveP0     = metrics?.live_p0_count ?? 0
  const liveP1     = metrics?.live_p1_count ?? 0
  const liveTotal  = metrics?.live_total_bugs ?? 0
  const triageTod  = metrics?.triaged_today ?? 0
  const needsTriage= metrics?.needs_triage ?? 0
  const online     = metrics?.sources_online ?? 0
  const total      = metrics?.sources_total ?? 0
  const triaged    = metrics?.total_triages ?? metrics?.total_triaged ?? 0
  const recentAct  = metrics?.recent_activity || []
  const toPercent  = (s) => s == null ? 0 : s > 1 ? Math.min(Math.round(s), 100) : Math.min(Math.round(s * 100), 100)
  const avgConf    = metrics?.avg_confidence != null ? toPercent(metrics.avg_confidence) : null

  const liveP2 = Math.max(0, liveTotal - liveP0 - liveP1)

  return (
    <div>
      <div className="page-hdr-row">
        <div className="page-hdr">
          <h1>System Overview</h1>
          <p>Auto-discovery active · {online} source{online !== 1 ? 's' : ''} online</p>
        </div>
      </div>

      {/* Live stats strip */}
      <div style={{
        display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap',
        background: 'var(--bg)', border: '1px solid var(--border)',
        borderRadius: 10, padding: '10px 16px', alignItems: 'center',
        fontSize: 13, fontWeight: 500,
      }}>
        <span style={{ color: 'var(--red)', fontWeight: 700 }}>🔴 P0: {liveP0}</span>
        <span style={{ color: 'var(--text3)' }}>·</span>
        <span style={{ color: '#D97706', fontWeight: 700 }}>🟠 P1: {liveP1}</span>
        <span style={{ color: 'var(--text3)' }}>·</span>
        <span style={{ color: 'var(--green)', fontWeight: 700 }}>✅ Triaged Today: {triageTod}</span>
        <span style={{ color: 'var(--text3)' }}>·</span>
        <span style={{ color: '#D97706' }}>⏳ Needs Triage: {needsTriage}</span>
        <span style={{ color: 'var(--text3)' }}>·</span>
        <span style={{ color: 'var(--teal)', fontWeight: 700 }}>
          🟢 {online}/{total} Systems Online
        </span>
        {liveTotal > 0 && (
          <>
            <span style={{ color: 'var(--text3)' }}>·</span>
            <span style={{ color: 'var(--text2)', fontSize: 12 }}>
              {liveTotal} live bugs
            </span>
          </>
        )}
      </div>

      {/* Stat cards */}
      <div className="stat-grid">
        <StatCard label="P0 Critical"    value={liveP0}                                color="red"   topBorder="red-t"   sub="live from connectors" />
        <StatCard label="P1 High"        value={liveP1}                                color="amber" topBorder="amber-t" sub="live from connectors" />
        <StatCard label="Triaged Today"  value={triageTod}                             color="green" topBorder="green-t" sub="AI-processed today" />
        <StatCard label="Needs Triage"   value={needsTriage}                           color="amber" topBorder="amber-t" sub="backlog" />
        <StatCard label="Systems Online" value={online ? `${online}/${total}` : '—'}   color="teal"  topBorder="teal-t"  sub="connected sources" />
      </div>

      {/* Bug distribution summary */}
      {liveTotal > 0 && (
        <div className="card" style={{ marginBottom: 16, padding: '12px 20px' }}>
          <div style={{ display: 'flex', gap: 24, alignItems: 'center', flexWrap: 'wrap', fontSize: 13 }}>
            <span style={{ fontWeight: 700, color: 'var(--text2)' }}>Live Bug Distribution</span>
            <span>Total: <strong>{liveTotal}</strong></span>
            <span style={{ color: 'var(--red)' }}>P0: <strong>{liveP0}</strong></span>
            <span style={{ color: '#D97706' }}>P1: <strong>{liveP1}</strong></span>
            <span style={{ color: '#92400E' }}>P2+: <strong>{liveP2}</strong></span>
            {avgConf != null && (
              <span style={{ color: 'var(--teal)' }}>Avg AI Confidence: <strong>{avgConf}%</strong></span>
            )}
            <span style={{ color: 'var(--text3)' }}>All Triages: <strong>{triaged}</strong></span>
          </div>
        </div>
      )}

      {/* 2-col lower grid */}
      <div className="dash-grid">
        {/* By Source */}
        <div className="card">
          <div className="dash-panel-hdr">
            <h3>Connected Systems</h3>
            <span style={{ fontSize: 11, color: 'var(--text3)', fontFamily: 'JetBrains Mono, monospace' }}>{online} configured</span>
          </div>
          {Object.keys(metrics?.by_source || {}).length === 0 ? (
            <p style={{ color: 'var(--text3)', fontSize: 13, margin: 0 }}>
              {metrics ? 'No triage data yet.' : 'Loading…'}
            </p>
          ) : Object.entries(metrics.by_source).map(([srcId, count]) => (
            <div key={srcId} className="conn-item">
              <div className={`conn-icon-box ${SOURCE_CLS[srcId] || 'ci-jira'}`}>
                {SOURCE_ICON[srcId] || srcId.slice(0, 2).toUpperCase()}
              </div>
              <div className="conn-item-info">
                <strong>{srcId}</strong>
                <small>{count} triage{count !== 1 ? 's' : ''}</small>
              </div>
              <span className="status-dot ok" />
            </div>
          ))}
        </div>

        {/* Recent Triage Activity */}
        <div className="card">
          <div className="dash-panel-hdr">
            <h3>Recent Triage Activity</h3>
            <span style={{ fontSize: 11, color: 'var(--text3)', fontFamily: 'JetBrains Mono, monospace' }}>last {recentAct.length}</span>
          </div>
          {recentAct.length === 0 ? (
            <p style={{ color: 'var(--text3)', fontSize: 13, margin: 0 }}>No triage history yet.</p>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border)', color: 'var(--text3)' }}>
                  <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 600 }}>Bug ID</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 600 }}>Sev</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 600 }}>Conf</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 600 }}>Root Cause</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 600 }}>Dur</th>
                  <th style={{ textAlign: 'left', padding: '4px 8px', fontWeight: 600 }}>When</th>
                  <th style={{ padding: '4px 8px' }} />
                </tr>
              </thead>
              <tbody>
                {recentAct.map((entry, i) => {
                  const conf = entry.confidence != null ? `${toPercent(entry.confidence)}%` : '—'
                  const dur  = entry.duration_ms ? `${(entry.duration_ms / 1000).toFixed(1)}s` : '—'
                  return (
                    <tr key={entry.case_id || i} style={{ borderBottom: '1px solid var(--border)' }}>
                      <td style={{ padding: '5px 8px', fontFamily: 'JetBrains Mono, monospace', color: 'var(--text2)' }}>
                        {entry.bug_id}
                      </td>
                      <td style={{ padding: '5px 8px' }}>
                        <span className={`sev ${SEV_CLS[entry.severity] || 'sev-unk'}`}>{entry.severity || '?'}</span>
                      </td>
                      <td style={{ padding: '5px 8px', color: 'var(--teal)', fontFamily: 'JetBrains Mono, monospace' }}>{conf}</td>
                      <td style={{ padding: '5px 8px', color: 'var(--text3)', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {entry.root_cause || '—'}
                      </td>
                      <td style={{ padding: '5px 8px', color: 'var(--text3)', fontFamily: 'JetBrains Mono, monospace', whiteSpace: 'nowrap' }}>{dur}</td>
                      <td style={{ padding: '5px 8px', color: 'var(--text3)', whiteSpace: 'nowrap' }}>{timeAgo(entry.created_at)}</td>
                      <td style={{ padding: '5px 8px' }}>
                        {entry.case_id && (
                          <button
                            className="btn btn-ghost btn-sm"
                            style={{ fontSize: 11 }}
                            onClick={() => navigate(`/triage/${entry.case_id}?from=history`)}
                          >
                            View
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}
