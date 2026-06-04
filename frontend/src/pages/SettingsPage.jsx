import { useState, useEffect } from 'react'
import { useAuth } from '../context/AuthContext'
import { getConnections, addConnection, updateConnection, removeConnection, testConnection, listUsers, createUser, deleteUser } from '../api/settings'

const SYSTEM_TYPES = [
  { value: 'jira',        label: 'JIRA' },
  { value: 'github',      label: 'GitHub Issues' },
  { value: 'jira_apache', label: 'Apache JIRA' },
  { value: 'bugzilla',    label: 'Bugzilla' },
  { value: 'confluence',  label: 'Confluence' },
  { value: 'customer_portal', label: 'Customer Portal' },
  { value: 'support_kb',  label: 'Support KB' },
]

const BASE_URL_DEFAULTS = {
  github:      'https://api.github.com',
  jira_apache: 'https://issues.apache.org/jira',
  bugzilla:    'https://bugzilla.mozilla.org',
  confluence:  'https://cwiki.apache.org/confluence',
  customer_portal: 'http://localhost:8000/mock/customer-portal',
  support_kb:  'https://cpp3-hpe.atlassian.net/wiki',
}

const ICON_COLORS = {
  github:          { bg: '#5B3FA0', text: '#fff' },
  jira_apache:     { bg: '#1A56A0', text: '#fff' },
  bugzilla:        { bg: '#D97706', text: '#fff' },
  confluence:      { bg: '#0A7C6E', text: '#fff' },
  customer_portal: { bg: '#166534', text: '#fff' },
  support_kb:      { bg: '#0A7C6E', text: '#fff' },
}

const FILTER_OPTIONS = [
  { key: 'all',             label: 'All Systems',       typeKey: null },
  { key: 'jira',            label: 'JIRA',              typeKey: 'jira' },
  { key: 'github',          label: 'GitHub',            typeKey: 'github' },
  { key: 'jira_apache',     label: 'Apache JIRA',       typeKey: 'jira_apache' },
  { key: 'bugzilla',        label: 'Bugzilla',          typeKey: 'bugzilla' },
  { key: 'confluence',      label: 'Confluence',        typeKey: 'confluence' },
  { key: 'customer_portal', label: 'Customer Portal',   typeKey: 'customer_portal' },
  { key: 'support_kb',      label: 'Support KB',        typeKey: 'support_kb' },
]

const ROLE_COLORS = { admin: '#B91C1C', engineer: '#1A56A0', customer: '#D97706', executive: '#166534' }

const EMPTY_FORM = {
  display_name: '',
  system_type: 'github',
  base_url: BASE_URL_DEFAULTS.github,
  auth_type: 'bearer_token',
  auth_token: '',
  project_key: '',
  ticket_prefix: '',
}

const EMPTY_USER_FORM = { email: '', password: '', role: 'engineer', display_name: '' }

function Dot({ color }) {
  return (
    <span style={{
      display: 'inline-block', width: 8, height: 8, borderRadius: '50%',
      background: color, marginRight: 5, flexShrink: 0,
    }} />
  )
}

function StatusIndicator({ conn }) {
  if (!conn.enabled)
    return <span style={{ display: 'flex', alignItems: 'center', fontSize: 12, color: 'var(--text3)' }}><Dot color="#9AA3B5" />Disabled</span>
  if (conn.auth_type === 'none' || conn.system_type === 'jira_apache' || conn.system_type === 'bugzilla' ||
      conn.system_type === 'confluence' || conn.system_type === 'customer_portal' || conn.system_type === 'support_kb')
    return <span style={{ display: 'flex', alignItems: 'center', fontSize: 12, color: 'var(--green)' }}><Dot color="#166534" />Internal/Public</span>
  if (conn.token_present)
    return <span style={{ display: 'flex', alignItems: 'center', fontSize: 12, color: 'var(--green)' }}><Dot color="#166534" />Connected</span>
  return <span style={{ display: 'flex', alignItems: 'center', fontSize: 12, color: 'var(--red)' }}><Dot color="#B91C1C" />No Token</span>
}

function RoleBadge({ role }) {
  return (
    <span style={{
      fontSize: 11, fontWeight: 700, padding: '2px 8px', borderRadius: 10,
      background: (ROLE_COLORS[role] || '#6B7280') + '22',
      color: ROLE_COLORS[role] || '#6B7280',
      border: `1px solid ${ROLE_COLORS[role] || '#6B7280'}44`,
    }}>
      {role}
    </span>
  )
}

export default function SettingsPage() {
  const { user }                                    = useAuth()
  const [connections,    setConnections]            = useState([])
  const [byType,         setByType]                 = useState({})
  const [filter,         setFilter]                 = useState('all')
  const [testing,        setTesting]                = useState({})
  const [testResults,    setTestResults]            = useState({})
  const [showAddModal,   setShowAddModal]           = useState(false)
  const [addForm,        setAddForm]                = useState(EMPTY_FORM)
  const [addLoading,     setAddLoading]             = useState(false)
  const [addError,       setAddError]               = useState('')
  const [toast,          setToast]                  = useState('')

  // User management state
  const [users,          setUsers]                  = useState([])
  const [usersLoading,   setUsersLoading]           = useState(false)
  const [showUserModal,  setShowUserModal]          = useState(false)
  const [userForm,       setUserForm]               = useState(EMPTY_USER_FORM)
  const [userLoading,    setUserLoading]            = useState(false)
  const [userError,      setUserError]              = useState('')

  const [editingId,      setEditingId]              = useState(null)
  const [editForm,       setEditForm]               = useState({
    display_name: '',
    base_url: '',
    project_key: '',
    token: '',
  })

  const isAdmin = user?.role === 'admin'

  const fetchConnections = () => {
    getConnections()
      .then((data) => {
        setConnections(data.connections || [])
        setByType(data.by_type || {})
      })
      .catch(console.error)
  }

  const fetchUsers = () => {
    if (!isAdmin) return
    setUsersLoading(true)
    listUsers()
      .then(setUsers)
      .catch(console.error)
      .finally(() => setUsersLoading(false))
  }

  useEffect(() => {
    fetchConnections()
    fetchUsers()
  }, [isAdmin])

  const showToast = (msg) => { setToast(msg); setTimeout(() => setToast(''), 3000) }

  const handleTest = async (sourceId) => {
    setTesting((p) => ({ ...p, [sourceId]: true }))
    try {
      const r = await testConnection(sourceId)
      setTestResults((p) => ({ ...p, [sourceId]: r }))
    } catch (err) {
      setTestResults((p) => ({
        ...p,
        [sourceId]: { status: 'error', message: err.response?.data?.detail || err.message || 'Connection failed' },
      }))
    } finally {
      setTesting((p) => ({ ...p, [sourceId]: false }))
    }
  }

  const handleRemove = async (sourceId) => {
    if (!window.confirm('Remove this connection?')) return
    try {
      await removeConnection(sourceId)
      fetchConnections()
    } catch (err) {
      alert('Failed to remove: ' + (err.response?.data?.detail || err.message))
    }
  }

  const handleEditClick = (connector) => {
    setEditingId(connector.source_id)
    setEditForm({
      display_name: connector.display_name || '',
      base_url: connector.base_url || '',
      project_key: connector.project_key || '',
      ticket_prefix: connector.ticket_prefix || '',
      auth_type: connector.auth_type || 'bearer_token',
      token: '',
    })
  }

  const handleEditSave = async (source_id) => {
    try {
      await updateConnection(source_id, editForm)
      setEditingId(null)
      fetchConnections()
    } catch (err) {
      alert('Error updating connection: ' + (err.response?.data?.detail || err.message))
    }
  }

  const handleAddSubmit = async (e) => {
    e.preventDefault()
    setAddLoading(true)
    setAddError('')
    try {
      await addConnection(addForm)
      setShowAddModal(false)
      setAddForm(EMPTY_FORM)
      fetchConnections()
      showToast('Connection added successfully')
    } catch (err) {
      setAddError(err.response?.data?.detail || err.message || 'Failed to add connection')
    } finally {
      setAddLoading(false)
    }
  }

  const handleSystemTypeChange = (type) => {
    setAddForm((f) => ({ ...f, system_type: type, base_url: BASE_URL_DEFAULTS[type] || '' }))
  }

  const handleAddUser = async (e) => {
    e.preventDefault()
    setUserLoading(true)
    setUserError('')
    try {
      await createUser(userForm)
      setShowUserModal(false)
      setUserForm(EMPTY_USER_FORM)
      fetchUsers()
      showToast('User created successfully')
    } catch (err) {
      setUserError(err.response?.data?.detail || err.message || 'Failed to create user')
    } finally {
      setUserLoading(false)
    }
  }

  const handleDeleteUser = async (email) => {
    if (!window.confirm(`Delete user ${email}?`)) return
    try {
      await deleteUser(email)
      fetchUsers()
      showToast(`User ${email} deleted`)
    } catch (err) {
      alert('Failed to delete: ' + (err.response?.data?.detail || err.message))
    }
  }

  const closeModal = () => { setShowAddModal(false); setAddError('') }
  const closeUserModal = () => { setShowUserModal(false); setUserError('') }

  const filtered = filter === 'all'
    ? connections
    : connections.filter((c) => c.system_type === filter)

  const filterLabel = FILTER_OPTIONS.find((f) => f.key === filter)?.label || 'All Systems'

  return (
    <div>
      {toast && (
        <div style={{
          position: 'fixed', top: 20, right: 20, zIndex: 9999,
          background: 'var(--green)', color: '#fff', padding: '10px 18px',
          borderRadius: 8, fontSize: 13, fontWeight: 600,
          boxShadow: '0 4px 16px rgba(0,0,0,0.15)',
        }}>
          ✓ {toast}
        </div>
      )}

      <div className="page-hdr">
        <h1>Connections</h1>
        <p>Manage source system connectors</p>
      </div>

      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
        {/* LEFT SIDEBAR */}
        <div style={{
          width: 220, flexShrink: 0,
          background: '#fff', border: '1px solid var(--border)',
          borderRadius: 10, overflow: 'hidden',
        }}>
          <div style={{
            padding: '12px 16px 8px',
            fontSize: 10.5, fontWeight: 700, color: 'var(--text3)',
            letterSpacing: '0.07em', textTransform: 'uppercase',
          }}>
            Filter by System
          </div>
          {FILTER_OPTIONS.map((opt) => {
            const count = opt.typeKey === null ? connections.length : (byType[opt.typeKey] || 0)
            const active = filter === opt.key
            return (
              <button
                key={opt.key}
                onClick={() => setFilter(opt.key)}
                style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  width: '100%', padding: '9px 16px', textAlign: 'left',
                  border: 'none', borderRadius: 0, cursor: 'pointer', fontSize: 13.5,
                  background: active ? 'var(--teal-lt)' : 'transparent',
                  color: active ? 'var(--teal)' : 'var(--text)',
                  fontWeight: active ? 600 : 400,
                }}
              >
                <span>{opt.label}</span>
                <span style={{
                  fontSize: 11, fontWeight: 700, minWidth: 20, textAlign: 'center',
                  padding: '1px 7px', borderRadius: 10,
                  background: active ? 'var(--teal)' : 'var(--bg)',
                  color: active ? '#fff' : 'var(--text2)',
                }}>
                  {count}
                </span>
              </button>
            )
          })}
        </div>

        {/* RIGHT PANEL */}
        <div style={{ flex: 1 }}>
          <div style={{ background: '#fff', border: '1px solid var(--border)', borderRadius: 10, padding: 20, marginBottom: 20 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
              <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>{filterLabel}</h3>
              <button className="btn btn-teal btn-sm" onClick={() => setShowAddModal(true)}>
                + Add Connection
              </button>
            </div>

            <div style={{
              background: 'var(--blue-lt)', border: '1px solid var(--blue-bd)',
              borderRadius: 8, padding: '10px 14px', marginBottom: 16,
              fontSize: 12, color: 'var(--blue)', display: 'flex', alignItems: 'flex-start', gap: 8,
            }}>
              <span style={{ flexShrink: 0, fontWeight: 700 }}>ℹ</span>
              <span>
                Connectors seeded via{' '}
                <code style={{ fontFamily: 'JetBrains Mono, monospace' }}>init_db.py</code>
                {' '}on startup. Changes take effect immediately.
              </span>
            </div>

            {filtered.length === 0 ? (
              <p style={{ textAlign: 'center', color: 'var(--text3)', fontSize: 13, padding: '32px 0' }}>
                No connections found for this system type.
              </p>
            ) : filtered.map((conn) => {
              const ic = ICON_COLORS[conn.system_type] || { bg: '#9AA3B5', text: '#fff' }
              const testRes = testResults[conn.source_id]
              const isLoading = !!testing[conn.source_id]
              return (
                <div key={conn.source_id} style={{
                  border: '1px solid var(--border)', borderRadius: 10, padding: 16, marginBottom: 12,
                }}>
                  <div style={{ display: 'flex', alignItems: 'center' }}>
                    <div style={{
                      width: 36, height: 36, borderRadius: 8, flexShrink: 0,
                      background: ic.bg, color: ic.text,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontFamily: 'JetBrains Mono, monospace', fontSize: 10.5, fontWeight: 700,
                    }}>
                      {conn.icon}
                    </div>
                    <div style={{ flex: 1, margin: '0 16px', minWidth: 0 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                        <span style={{ fontWeight: 700, fontSize: 14 }}>{conn.display_name}</span>
                        {conn.ticket_prefix && (
                          <span style={{
                            fontSize: 10.5, fontFamily: 'JetBrains Mono, monospace',
                            border: '1px solid var(--teal-bd)', color: 'var(--teal)',
                            borderRadius: 4, padding: '1px 6px',
                          }}>
                            {conn.ticket_prefix}
                          </span>
                        )}
                      </div>
                      <div style={{
                        fontFamily: 'JetBrains Mono, monospace', fontSize: 11,
                        color: 'var(--text3)', marginTop: 2,
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      }}>
                        {conn.base_url}
                      </div>
                      {conn.project_key && (
                        <div style={{ fontSize: 11.5, color: 'var(--text2)', marginTop: 1 }}>{conn.project_key}</div>
                      )}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexShrink: 0 }}>
                      <StatusIndicator conn={conn} />
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => handleTest(conn.source_id)}
                        disabled={isLoading}
                      >
                        {isLoading ? '⟳ Testing…' : 'Test'}
                      </button>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => handleEditClick(conn)}
                      >
                        Edit
                      </button>
                      <button
                        className="btn btn-sm"
                        style={{ border: '1px solid var(--red-bd)', color: 'var(--red)', background: 'transparent' }}
                        onClick={() => handleRemove(conn.source_id)}
                      >
                        Remove
                      </button>
                    </div>
                  </div>
                  {testRes && !isLoading && (
                    <div style={{
                      marginTop: 10, paddingTop: 10, borderTop: '1px solid var(--border)', fontSize: 12,
                      color: testRes.status === 'ok' ? 'var(--green)' : 'var(--red)',
                      fontFamily: 'JetBrains Mono, monospace',
                    }}>
                      {testRes.status === 'ok' ? `✓ ${testRes.message}` : `✗ ${testRes.message}`}
                    </div>
                  )}
                  {editingId === conn.source_id && (
                    <div style={{
                      marginTop: 10, padding: 12,
                      background: 'var(--bg)', border: '1px solid var(--border)',
                      borderRadius: 8, display: 'flex', flexDirection: 'column', gap: 8,
                    }}>
                      <div>
                        <label className="form-label" style={{ fontSize: 11 }}>Display Name</label>
                        <input className="form-input" style={{ width: '100%' }}
                          value={editForm.display_name}
                          onChange={e => setEditForm({ ...editForm, display_name: e.target.value })} />
                      </div>
                      <div>
                        <label className="form-label" style={{ fontSize: 11 }}>Base URL</label>
                        <input className="form-input" style={{ width: '100%' }}
                          value={editForm.base_url}
                          onChange={e => setEditForm({ ...editForm, base_url: e.target.value })} />
                      </div>
                      <div>
                        <label className="form-label" style={{ fontSize: 11 }}>Project Key</label>
                        <input className="form-input" style={{ width: '100%' }}
                          value={editForm.project_key}
                          onChange={e => setEditForm({ ...editForm, project_key: e.target.value })} />
                      </div>
                      <div>
                        <label className="form-label" style={{ fontSize: 11 }}>Ticket Prefix</label>
                        <input className="form-input" style={{ width: '100%' }}
                          value={editForm.ticket_prefix}
                          onChange={e => setEditForm({ ...editForm, ticket_prefix: e.target.value })} />
                      </div>
                      <div>
                        <label className="form-label" style={{ fontSize: 11 }}>Auth Type</label>
                        <select className="form-select" style={{ width: '100%' }}
                          value={editForm.auth_type}
                          onChange={e => setEditForm({ ...editForm, auth_type: e.target.value })}>
                          <option value="bearer_token">Bearer Token</option>
                          <option value="basic">Basic</option>
                          <option value="pat">PAT</option>
                          <option value="none">None</option>
                        </select>
                      </div>
                      <div>
                        <label className="form-label" style={{ fontSize: 11 }}>New Token (leave blank to keep existing)</label>
                        <input className="form-input" style={{ width: '100%' }} type="password"
                          placeholder="Leave blank to keep existing token"
                          value={editForm.token}
                          onChange={e => setEditForm({ ...editForm, token: e.target.value })} />
                      </div>
                      <div style={{ display: 'flex', gap: 8 }}>
                        <button className="btn btn-teal btn-sm" onClick={() => handleEditSave(conn.source_id)}>
                          Save Changes
                        </button>
                        <button className="btn btn-ghost btn-sm" onClick={() => setEditingId(null)}>
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
          </div>

          {/* USER MANAGEMENT — admin only */}
          {isAdmin && (
            <div style={{ background: '#fff', border: '1px solid var(--border)', borderRadius: 10, padding: 20 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
                <div>
                  <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>User Management</h3>
                  <p style={{ margin: '4px 0 0', fontSize: 12, color: 'var(--text3)' }}>Admin only · {users.length} users</p>
                </div>
                <button className="btn btn-teal btn-sm" onClick={() => setShowUserModal(true)}>
                  + Add User
                </button>
              </div>

              {usersLoading ? (
                <p style={{ color: 'var(--text3)', fontSize: 13 }}>Loading users…</p>
              ) : (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--border)', color: 'var(--text3)' }}>
                      <th style={{ textAlign: 'left', padding: '6px 10px', fontWeight: 600 }}>Email</th>
                      <th style={{ textAlign: 'left', padding: '6px 10px', fontWeight: 600 }}>Display Name</th>
                      <th style={{ textAlign: 'left', padding: '6px 10px', fontWeight: 600 }}>Role</th>
                      <th style={{ textAlign: 'left', padding: '6px 10px', fontWeight: 600 }}>Created</th>
                      <th style={{ padding: '6px 10px' }} />
                    </tr>
                  </thead>
                  <tbody>
                    {users.map((u) => (
                      <tr key={u.user_id} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '8px 10px', fontFamily: 'JetBrains Mono, monospace', fontSize: 12 }}>
                          {u.user_id}
                        </td>
                        <td style={{ padding: '8px 10px', color: 'var(--text2)' }}>{u.display_name || '—'}</td>
                        <td style={{ padding: '8px 10px' }}><RoleBadge role={u.role} /></td>
                        <td style={{ padding: '8px 10px', color: 'var(--text3)', fontSize: 11 }}>
                          {u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}
                        </td>
                        <td style={{ padding: '8px 10px' }}>
                          {u.user_id !== user?.email && (
                            <button
                              className="btn btn-sm"
                              style={{ border: '1px solid var(--red-bd)', color: 'var(--red)', background: 'transparent', fontSize: 11 }}
                              onClick={() => handleDeleteUser(u.user_id)}
                            >
                              Delete
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>
      </div>

      {/* ADD CONNECTION MODAL */}
      {showAddModal && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
        }}>
          <div style={{
            background: '#fff', borderRadius: 14, padding: 32,
            maxWidth: 480, width: '100%', margin: '0 16px',
            position: 'relative', maxHeight: '90vh', overflowY: 'auto',
          }}>
            <button
              onClick={closeModal}
              style={{ position: 'absolute', top: 16, right: 16, background: 'none', border: 'none', fontSize: 22, cursor: 'pointer', color: 'var(--text3)' }}
            >×</button>
            <h2 style={{ margin: '0 0 24px', fontSize: 18, fontWeight: 700 }}>Add New Connection</h2>
            <form onSubmit={handleAddSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div>
                <label className="form-label">System Type</label>
                <select className="form-select" style={{ width: '100%' }} value={addForm.system_type}
                  onChange={(e) => handleSystemTypeChange(e.target.value)}>
                  {SYSTEM_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                </select>
              </div>
              <div>
                <label className="form-label">Display Name</label>
                <input className="form-input" style={{ width: '100%' }} placeholder="e.g. Apache Spark — GitHub"
                  value={addForm.display_name} onChange={(e) => setAddForm((f) => ({ ...f, display_name: e.target.value }))} required />
              </div>
              <div>
                <label className="form-label">Base URL</label>
                <input className="form-input" style={{ width: '100%' }} value={addForm.base_url}
                  onChange={(e) => setAddForm((f) => ({ ...f, base_url: e.target.value }))} required />
              </div>
              <div>
                <label className="form-label">Auth Token</label>
                <input className="form-input" style={{ width: '100%' }} type="password" placeholder="Leave empty for public APIs"
                  value={addForm.auth_token} onChange={(e) => setAddForm((f) => ({ ...f, auth_token: e.target.value }))} />
                <div style={{ fontSize: 11, color: 'var(--text3)', marginTop: 4 }}>Not required for Apache JIRA and Bugzilla</div>
              </div>
              <div>
                <label className="form-label">Auth Type</label>
                <select className="form-select" style={{ width: '100%' }} value={addForm.auth_type || 'bearer_token'}
                  onChange={(e) => setAddForm((f) => ({ ...f, auth_type: e.target.value }))}>
                  <option value="bearer_token">Bearer Token</option>
                  <option value="basic">Basic</option>
                  <option value="pat">PAT</option>
                  <option value="none">None</option>
                </select>
              </div>
              <div>
                <label className="form-label">Project Key</label>
                <input className="form-input" style={{ width: '100%' }} placeholder="e.g. apache/spark or SPARK"
                  value={addForm.project_key} onChange={(e) => setAddForm((f) => ({ ...f, project_key: e.target.value }))} />
              </div>
              <div>
                <label className="form-label">Ticket Prefix</label>
                <input className="form-input" style={{ width: '100%' }} placeholder="e.g. SGH or SPARK"
                  value={addForm.ticket_prefix} onChange={(e) => setAddForm((f) => ({ ...f, ticket_prefix: e.target.value }))} />
              </div>
              {addError && <div style={{ color: 'var(--red)', fontSize: 13 }}>{addError}</div>}
              <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
                <button type="button" className="btn btn-ghost btn-sm" onClick={closeModal}>Cancel</button>
                <button type="submit" className="btn btn-teal" disabled={addLoading}>
                  {addLoading ? 'Saving…' : 'Save & Test'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* ADD USER MODAL */}
      {showUserModal && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
        }}>
          <div style={{
            background: '#fff', borderRadius: 14, padding: 32,
            maxWidth: 420, width: '100%', margin: '0 16px',
            position: 'relative',
          }}>
            <button
              onClick={closeUserModal}
              style={{ position: 'absolute', top: 16, right: 16, background: 'none', border: 'none', fontSize: 22, cursor: 'pointer', color: 'var(--text3)' }}
            >×</button>
            <h2 style={{ margin: '0 0 24px', fontSize: 18, fontWeight: 700 }}>Add New User</h2>
            <form onSubmit={handleAddUser} style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div>
                <label className="form-label">Email</label>
                <input className="form-input" style={{ width: '100%' }} type="email" placeholder="user@company.com"
                  value={userForm.email} onChange={(e) => setUserForm((f) => ({ ...f, email: e.target.value }))} required />
              </div>
              <div>
                <label className="form-label">Password</label>
                <input className="form-input" style={{ width: '100%' }} type="password"
                  value={userForm.password} onChange={(e) => setUserForm((f) => ({ ...f, password: e.target.value }))} required />
              </div>
              <div>
                <label className="form-label">Role</label>
                <select className="form-select" style={{ width: '100%' }} value={userForm.role}
                  onChange={(e) => setUserForm((f) => ({ ...f, role: e.target.value }))}>
                  <option value="engineer">Engineer</option>
                  <option value="customer">Customer</option>
                  <option value="executive">Executive</option>
                  <option value="admin">Admin</option>
                </select>
              </div>
              <div>
                <label className="form-label">Display Name</label>
                <input className="form-input" style={{ width: '100%' }} placeholder="Full Name (optional)"
                  value={userForm.display_name} onChange={(e) => setUserForm((f) => ({ ...f, display_name: e.target.value }))} />
              </div>
              {userError && <div style={{ color: 'var(--red)', fontSize: 13 }}>{userError}</div>}
              <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
                <button type="button" className="btn btn-ghost btn-sm" onClick={closeUserModal}>Cancel</button>
                <button type="submit" className="btn btn-teal" disabled={userLoading}>
                  {userLoading ? 'Creating…' : 'Create User'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}
