import { useCallback, useEffect, useRef, useState } from 'react'
import { adminApi } from './adminApi'
import ChipInput from './ChipInput'

// restrict_values is a list for this app's portals; tolerate the
// {COLUMN: [values]} shape too so nothing renders as "[object Object]".
function accessChips(restrictValues, col) {
  if (Array.isArray(restrictValues)) return restrictValues.map(v => (col ? `${col}: ${v}` : v))
  return Object.entries(restrictValues || {}).flatMap(([k, vs]) => (Array.isArray(vs) ? vs.map(v => `${k}: ${v}`) : []))
}

const toList = restrictValues =>
  Array.isArray(restrictValues) ? restrictValues : Object.values(restrictValues || {}).flat()

export default function AdminUsersTab({ user, portals, portalId, setPortalId, onChanged }) {
  const [access, setAccess]   = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState('')
  const [notice, setNotice]   = useState('')
  const [editing, setEditing] = useState(null)        // email being edited, or null
  const [emails, setEmails]   = useState('')
  const [values, setValues]   = useState([])
  const [saving, setSaving]   = useState(false)
  const formRef = useRef(null)

  const portal = portals.find(p => p.id === portalId)
  const col = portal?.config?.restrict_col || ''

  const loadAccess = useCallback(async id => {
    if (!id) { setAccess([]); return }
    setLoading(true)
    try {
      const data = await adminApi(user, `/admin/portals/${encodeURIComponent(id)}/access`)
      setAccess(data.access || [])
    } catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }, [user])

  useEffect(() => {
    setError(''); setNotice(''); setEditing(null); setEmails(''); setValues([])
    loadAccess(portalId)
  }, [portalId, loadAccess])

  const reset = () => { setEditing(null); setEmails(''); setValues([]) }

  const startEdit = a => {
    setError(''); setNotice('')
    setEditing(a.email); setEmails(a.email); setValues(toList(a.restrict_values))
    formRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  const save = async () => {
    const list = emails.split(/[,;\s]+/).map(e => e.trim()).filter(Boolean)
    if (!list.length) { setError('Enter at least one email address.'); return }
    setSaving(true); setError(''); setNotice('')
    try {
      const res = await adminApi(user, `/portals/${encodeURIComponent(portalId)}/access`, {
        method: 'POST', body: { emails: list, restrict_values: values },
      })
      setNotice(editing ? `Updated ${editing}.` : `Added ${res.count} user${res.count === 1 ? '' : 's'}.`)
      reset()
      await loadAccess(portalId)
      onChanged()
    } catch (e) { setError(e.message) }
    finally { setSaving(false) }
  }

  const remove = async email => {
    if (!window.confirm(`Remove ${email} from ${portal.name}?`)) return
    setError(''); setNotice('')
    try {
      await adminApi(user, `/admin/portals/${encodeURIComponent(portalId)}/access?email=${encodeURIComponent(email)}`, { method: 'DELETE' })
      if (editing === email) reset()
      await loadAccess(portalId)
      onChanged()
    } catch (e) { setError(e.message) }
  }

  if (!portals.length) return <div className="ad-empty">No portals yet. Create one in the Portals tab first.</div>

  return (
    <div className="ad-tabpane">
      <div className="ad-card ad-bar">
        <label className="ad-bar-field">Portal:
          <select value={portalId || ''} onChange={e => setPortalId(e.target.value)}>
            {portals.map(p => <option key={p.id} value={p.id}>{p.name}{p.is_active ? '' : ' (inactive)'}</option>)}
          </select>
        </label>
        {portal && <code className="ad-table-chip">{portal.config?.target_table}</code>}
        {col
          ? <span className="ad-restrict-chip">🔒 Restrict by: <strong>{col}</strong></span>
          : <span className="ad-muted">No restriction column — every user can upload any rows</span>}
        <span className="ad-bar-count">{access.length} user{access.length === 1 ? '' : 's'}</span>
      </div>

      {error && <div className="error-bar"><strong>Error:</strong> {error}</div>}
      {notice && <div className="ad-notice">{notice}</div>}

      <div className="ad-card ad-tablecard">
        <table className="ad-table">
          <thead>
            <tr><th>Email</th><th>{col ? `${col} Access` : 'Access'}</th><th className="ad-right">Actions</th></tr>
          </thead>
          <tbody>
            {loading && access.length === 0 && <tr><td colSpan="3" className="ad-muted">Loading…</td></tr>}
            {!loading && access.length === 0 && (
              <tr><td colSpan="3" className="ad-muted">No users yet. Admins can always upload to every portal.</td></tr>
            )}
            {access.map(a => {
              const chips = accessChips(a.restrict_values, col)
              return (
                <tr key={a.email} className={editing === a.email ? 'ad-row-editing' : ''}>
                  <td>{a.email}</td>
                  <td>
                    <div className="ad-chips">
                      {chips.length
                        ? chips.map(c => <span key={c} className="ad-chip">{c}</span>)
                        : <span className="ad-chip ad-chip-all">All rows</span>}
                    </div>
                  </td>
                  <td className="ad-right ad-actions-cell">
                    <button className="ad-btn-sm" onClick={() => startEdit(a)}>Edit</button>
                    <button className="ad-btn-sm ad-btn-danger" onClick={() => remove(a.email)}>Remove</button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="ad-card ad-addcard" ref={formRef}>
        <div className="ad-card-head">{editing ? `Edit access for ${editing}` : `Add User to ${portal?.name}`}</div>
        <div className="ad-addgrid">
          <label>Email {editing ? '' : <span className="ad-muted">(comma-separated for multiple)</span>}
            <textarea rows="2" value={emails} disabled={!!editing} onChange={e => setEmails(e.target.value)}
                      placeholder="name@arvindfashions.com, other@arvindfashions.com" />
          </label>
          <label>{col || 'Restriction'} <span className="ad-muted">(empty = all)</span>
            <ChipInput value={values} onChange={setValues} disabled={!col}
                       placeholder={col ? `Type a ${col} value, press Enter` : 'This portal has no restriction column'} />
            {col && <span className="ad-hint">Type each value exactly as it appears in the uploaded files (case-sensitive).</span>}
          </label>
        </div>
        <div className="ad-formactions">
          {editing && <button className="btn-outline" onClick={reset} disabled={saving}>Cancel</button>}
          <button className="btn-primary" onClick={save} disabled={saving || !emails.trim()}>
            {saving ? 'Saving…' : editing ? 'Save changes' : 'Add user'}
          </button>
        </div>
      </div>
    </div>
  )
}
