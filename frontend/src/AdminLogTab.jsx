import { useCallback, useEffect, useState } from 'react'
import { adminApi, formatTime } from './adminApi'

// details is stored as a JSON string; show it as "key: value · key: value".
function summarise(details) {
  try {
    const obj = JSON.parse(details)
    if (obj && typeof obj === 'object') {
      return Object.entries(obj)
        .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : typeof v === 'object' && v ? JSON.stringify(v) : v}`)
        .join(' · ')
    }
  } catch { /* fall through */ }
  return details || ''
}

export default function AdminLogTab({ user }) {
  const [logs, setLogs]       = useState([])
  const [actions, setActions] = useState([])
  const [action, setAction]   = useState('')
  const [q, setQ]             = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState('')

  const load = useCallback(async (nextAction = action, nextQ = q) => {
    setLoading(true); setError('')
    try {
      const params = new URLSearchParams({ limit: '300' })
      if (nextAction) params.set('action', nextAction)
      if (nextQ.trim()) params.set('q', nextQ.trim())
      const data = await adminApi(user, `/admin/logs?${params}`)
      setLogs(data.logs || []); setActions(data.actions || [])
    } catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }, [user, action, q])

  useEffect(() => { load('', '') }, [user]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="ad-tabpane">
      <div className="ad-card ad-bar">
        <label className="ad-bar-field">Action:
          <select value={action} onChange={e => { setAction(e.target.value); load(e.target.value, q) }}>
            <option value="">All</option>
            {actions.map(a => <option key={a} value={a}>{a}</option>)}
          </select>
        </label>
        <input className="ad-search" value={q} placeholder="Search user or details…"
               onChange={e => setQ(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') load() }} />
        <button className="btn-outline" onClick={() => load()} disabled={loading}>{loading ? 'Loading…' : 'Refresh'}</button>
        <span className="ad-bar-count">latest {logs.length}</span>
      </div>

      {error && <div className="error-bar"><strong>Error:</strong> {error}</div>}

      <div className="ad-card ad-tablecard">
        <table className="ad-table">
          <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Details</th></tr></thead>
          <tbody>
            {!loading && logs.length === 0 && <tr><td colSpan="4" className="ad-muted">Nothing matches.</td></tr>}
            {logs.map(l => {
              const text = summarise(l.details)
              return (
                <tr key={l.id}>
                  <td className="ad-nowrap">{formatTime(l.ts)}</td>
                  <td>{l.email || '—'}</td>
                  <td><span className="ad-chip">{l.action}</span></td>
                  <td className="ad-details" title={text}>{text}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
