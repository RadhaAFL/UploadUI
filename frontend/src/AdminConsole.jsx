import { useCallback, useEffect, useMemo, useState } from 'react'
import { msalInstance } from './authConfig'
import arvindLogo from './assets/arvind-logo.png'
import { logEvent } from './logger'
import './AdminConsole.css'

const API = '/uploadportal-api'

// Admin endpoints identify the caller with `caller_email` (see _require_admin
// in app.py): query string for GET/DELETE, JSON body for POST/PUT.
async function adminApi(user, path, { method = 'GET', body } = {}) {
  const email = encodeURIComponent(user.email)
  const url = body ? `${API}${path}` : `${API}${path}${path.includes('?') ? '&' : '?'}caller_email=${email}`
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify({ ...body, caller_email: user.email }) : undefined,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.error || `Server error ${res.status}`)
  return data
}

const blankColumn = () => ({ key: '', label: '', numeric: false, example: '', description: '' })

const emptyForm = () => ({
  id: '', name: '', description: '', is_active: true,
  target_table: '', load_strategy: 'append', columns: [blankColumn()],
  date_column: '', date_format: '', restrict_col: '',
  use_period: false, cutover_day: 25,
  audit_file_name: '', audit_run_date: '', audit_load_run_date: '',
})

function formFromPortal(p) {
  const c = p.config || {}
  const audit = c.audit_columns || {}
  return {
    id: p.id, name: p.name, description: p.description || '', is_active: p.is_active,
    target_table: c.target_table || '', load_strategy: p.load_strategy,
    columns: (c.columns?.length ? c.columns : [blankColumn()]).map(col => ({
      key: col.key, label: col.label || '', numeric: !!col.numeric,
      example: col.example || '', description: col.description || '',
    })),
    date_column: c.date_column || '', date_format: c.date_format || '', restrict_col: c.restrict_col || '',
    use_period: !!c.period_rule, cutover_day: c.period_rule?.cutover_day ?? 25,
    audit_file_name: audit.file_name || '', audit_run_date: audit.run_date || '',
    audit_load_run_date: audit.load_run_date || '',
  }
}

function configFromForm(f, needsPeriod) {
  const cfg = {
    target_table: f.target_table.trim(),
    load_strategy: f.load_strategy,
    columns: f.columns.filter(c => c.key.trim()).map(c => ({
      key: c.key.trim(), label: c.label.trim(), numeric: c.numeric,
      example: c.example.trim(), description: c.description.trim(),
    })),
  }
  if (f.date_column) cfg.date_column = f.date_column
  if (f.date_format.trim()) cfg.date_format = f.date_format.trim()
  if (f.restrict_col) cfg.restrict_col = f.restrict_col
  if (needsPeriod || f.use_period) cfg.period_rule = { unit: 'month', cutover_day: Number(f.cutover_day) || 25 }
  const audit = {}
  if (f.audit_file_name.trim()) audit.file_name = f.audit_file_name.trim()
  if (f.audit_run_date.trim()) audit.run_date = f.audit_run_date.trim()
  if (f.audit_load_run_date.trim()) audit.load_run_date = f.audit_load_run_date.trim()
  if (Object.keys(audit).length) cfg.audit_columns = audit
  return cfg
}

const STRATEGY_BADGE = { replace_period: 'Replace period', truncate_load: 'Truncate & load', append: 'Append' }

export default function AdminConsole({ user, onBack }) {
  const [portals, setPortals]       = useState([])
  const [strategies, setStrategies] = useState([])
  const [loading, setLoading]       = useState(true)
  const [selected, setSelected]     = useState(null)   // portal id | 'new' | null
  const [form, setForm]             = useState(null)
  const [saving, setSaving]         = useState(false)
  const [error, setError]           = useState('')
  const [notice, setNotice]         = useState('')
  const [access, setAccess]         = useState([])
  const [newAccess, setNewAccess]   = useState({ email: '', values: '' })
  const [admins, setAdmins]         = useState([])
  const [newAdmin, setNewAdmin]     = useState('')
  const [adminError, setAdminError] = useState('')
  const me = user.email.toLowerCase()

  const loadPortals = useCallback(async () => {
    const data = await adminApi(user, '/admin/portals')
    setPortals(data.portals || [])
    return data.portals || []
  }, [user])

  const loadAdmins = useCallback(async () => {
    try {
      const data = await adminApi(user, '/admin/admins')
      setAdmins(data.admins || [])
    } catch (e) { setAdminError(e.message) }
  }, [user])

  const addAdmin = async () => {
    const email = newAdmin.trim().toLowerCase()
    if (!email) return
    setAdminError('')
    try {
      await adminApi(user, '/admin/admins', { method: 'POST', body: { email } })
      logEvent(user, 'admin_add', { email })
      setNewAdmin('')
      await loadAdmins()
    } catch (e) { setAdminError(e.message) }
  }

  const removeAdmin = async email => {
    if (!window.confirm(`Remove admin access for ${email}? They will still be able to use any portals they've been given access to.`)) return
    setAdminError('')
    try {
      await adminApi(user, `/admin/admins?email=${encodeURIComponent(email)}`, { method: 'DELETE' })
      logEvent(user, 'admin_remove', { email })
      await loadAdmins()
    } catch (e) { setAdminError(e.message) }
  }

  useEffect(() => {
    loadAdmins()
    Promise.all([adminApi(user, '/admin/strategies'), loadPortals()])
      .then(([s]) => setStrategies(s.strategies || []))
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [user, loadPortals, loadAdmins])

  const loadAccess = useCallback(async id => {
    try {
      const data = await adminApi(user, `/admin/portals/${encodeURIComponent(id)}/access`)
      setAccess(data.access || [])
    } catch (e) { setError(e.message) }
  }, [user])

  const selectPortal = p => {
    setError(''); setNotice('')
    setSelected(p.id); setForm(formFromPortal(p)); setAccess([])
    loadAccess(p.id)
  }

  const startNew = () => {
    setError(''); setNotice('')
    setSelected('new'); setForm(emptyForm()); setAccess([])
  }

  const set = patch => setForm(f => ({ ...f, ...patch }))
  const setColumn = (i, patch) =>
    setForm(f => ({ ...f, columns: f.columns.map((c, j) => (j === i ? { ...c, ...patch } : c)) }))
  const addColumn = () => setForm(f => ({ ...f, columns: [...f.columns, blankColumn()] }))
  const removeColumn = i => setForm(f => ({ ...f, columns: f.columns.filter((_, j) => j !== i) }))

  const strategyInfo = strategies.find(s => s.id === form?.load_strategy)
  const needsPeriod = strategyInfo ? strategyInfo.needs_period : form?.load_strategy === 'replace_period'
  const columnKeys = useMemo(
    () => (form?.columns || []).map(c => c.key.trim().toUpperCase()).filter(Boolean),
    [form?.columns],
  )
  const existing = portals.find(p => p.id === selected)
  const isNew = selected === 'new'

  const save = async () => {
    setSaving(true); setError(''); setNotice('')
    try {
      const config = configFromForm(form, needsPeriod)
      if (isNew) {
        await adminApi(user, '/admin/portals', {
          method: 'POST', body: { id: form.id.trim(), name: form.name, description: form.description, config },
        })
        logEvent(user, 'portal_create', { portal_id: form.id.trim(), strategy: form.load_strategy })
        await loadPortals()
        setSelected(form.id.trim())
        setNotice('Portal created. Add the users who may use it below.')
        loadAccess(form.id.trim())
      } else {
        await adminApi(user, `/admin/portals/${encodeURIComponent(form.id)}`, {
          method: 'PUT',
          body: { name: form.name, description: form.description, config, is_active: form.is_active },
        })
        logEvent(user, 'portal_update', { portal_id: form.id, strategy: form.load_strategy })
        await loadPortals()
        setNotice('Saved.')
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  const addAccess = async () => {
    const email = newAccess.email.trim().toLowerCase()
    if (!email) return
    setError('')
    try {
      const values = newAccess.values.split(',').map(v => v.trim()).filter(Boolean)
      await adminApi(user, `/portals/${encodeURIComponent(form.id)}/access`, {
        method: 'POST', body: { email, restrict_values: values },
      })
      setNewAccess({ email: '', values: '' })
      await loadAccess(form.id)
      loadPortals()
    } catch (e) { setError(e.message) }
  }

  const removeAccess = async email => {
    setError('')
    try {
      await adminApi(user, `/admin/portals/${encodeURIComponent(form.id)}/access?email=${encodeURIComponent(email)}`, { method: 'DELETE' })
      await loadAccess(form.id)
      loadPortals()
    } catch (e) { setError(e.message) }
  }

  const hookFile = existing?.hook_file || `backend/portals/${form?.id || '<portal-id>'}.py`

  return (
    <div className="up-page">
      <header className="kpi-header">
        <div className="kpi-header-left">
          <img src={arvindLogo} alt="Arvind Fashions" className="kpi-logo" />
          <button className="btn-back-portals" onClick={onBack}>Back to Portals</button>
          <div>
            <div className="kpi-title">Admin console</div>
            <div className="kpi-subtitle">Define upload portals, load rules and access</div>
          </div>
        </div>
        <div className="kpi-header-right">
          <span className="kpi-user">{user.displayName}</span>
          <button className="btn-signout" onClick={() => msalInstance.logoutRedirect()}>Sign out</button>
        </div>
      </header>

      <main className="ad-layout">
        <aside className="ad-list">
          <button className="btn-primary ad-new" onClick={startNew}>+ New portal</button>
          {loading && <div className="ad-muted">Loading…</div>}
          {portals.map(p => (
            <div key={p.id}
                 className={`ad-item ${selected === p.id ? 'ad-item-active' : ''} ${p.is_active ? '' : 'ad-item-off'}`}
                 onClick={() => selectPortal(p)}>
              <div className="ad-item-name">{p.name}</div>
              <div className="ad-item-meta">
                <span className={`ad-badge ad-badge-${p.load_strategy}`}>{STRATEGY_BADGE[p.load_strategy] || p.load_strategy}</span>
                {p.has_hooks && <span className="ad-badge ad-badge-hook">Python hook</span>}
                {!p.is_active && <span className="ad-badge ad-badge-off">Inactive</span>}
              </div>
              <div className="ad-item-table">{p.config?.target_table} · {p.access_count} user{p.access_count === 1 ? '' : 's'}</div>
            </div>
          ))}

          <div className="ad-card ad-admins">
            <div className="ad-card-head">Administrators</div>
            <div className="ad-muted">Admins can create portals, change load rules and manage access and other admins.</div>
            {admins.map(a => (
              <div key={a} className="ad-admin-row">
                <span className="ad-admin-email">{a}{a === me && <span className="ad-you"> (you)</span>}</span>
                <button className="ad-x" disabled={a === me}
                        title={a === me ? "You can't remove your own access" : `Remove ${a}`}
                        onClick={() => removeAdmin(a)}>Remove</button>
              </div>
            ))}
            <div className="ad-admin-add">
              <input value={newAdmin} onChange={e => setNewAdmin(e.target.value)}
                     onKeyDown={e => { if (e.key === 'Enter') addAdmin() }}
                     placeholder="name@arvindfashions.com" />
              <button className="btn-outline" onClick={addAdmin} disabled={!newAdmin.trim()}>Add admin</button>
            </div>
            {adminError && <div className="ad-inline-error">{adminError}</div>}
          </div>
        </aside>

        <section className="ad-editor">
          {error && <div className="error-bar"><strong>Error:</strong> {error}</div>}
          {notice && <div className="ad-notice">{notice}</div>}

          {!form && !loading && (
            <div className="ad-empty">Select a portal to edit it, or create a new one.</div>
          )}

          {form && (
            <>
              <div className="ad-card">
                <div className="ad-card-head">Portal</div>
                <div className="ad-grid">
                  <label>Name
                    <input value={form.name} onChange={e => set({ name: e.target.value })} placeholder="Store Master Upload" />
                  </label>
                  <label>Portal id {isNew ? '' : '(fixed)'}
                    <input value={form.id} disabled={!isNew}
                           onChange={e => set({ id: e.target.value.toLowerCase() })} placeholder="store-master" />
                  </label>
                  <label className="ad-span2">Description
                    <input value={form.description} onChange={e => set({ description: e.target.value })}
                           placeholder="Shown to users under the portal name" />
                  </label>
                  {!isNew && (
                    <label className="ad-check">
                      <input type="checkbox" checked={form.is_active} onChange={e => set({ is_active: e.target.checked })} />
                      Active (inactive portals are hidden from all users)
                    </label>
                  )}
                </div>
              </div>

              <div className="ad-card">
                <div className="ad-card-head">How the data is loaded</div>
                <div className="ad-grid">
                  <label>Target table (Fabric)
                    <input value={form.target_table} onChange={e => set({ target_table: e.target.value })}
                           placeholder="prd.DIM_STORE_MASTER" />
                  </label>
                  <label>Load strategy
                    <select value={form.load_strategy} onChange={e => set({ load_strategy: e.target.value })}>
                      {(strategies.length ? strategies : [{ id: form.load_strategy, label: form.load_strategy }]).map(s =>
                        <option key={s.id} value={s.id}>{s.label}</option>)}
                    </select>
                  </label>
                  {strategyInfo && <div className="ad-hint ad-span2">{strategyInfo.summary}</div>}

                  {(needsPeriod || form.use_period) && (
                    <label>Cutover day of month
                      <input type="number" min="1" max="31" value={form.cutover_day}
                             onChange={e => set({ cutover_day: e.target.value })} />
                      <span className="ad-hint">Before this day uploads must be for the current month; from this day, for next month.</span>
                    </label>
                  )}
                  {!needsPeriod && (
                    <label className="ad-check">
                      <input type="checkbox" checked={form.use_period} onChange={e => set({ use_period: e.target.checked })} />
                      Also require the file's dates to fall in the expected month
                    </label>
                  )}

                  <label>Date column
                    <select value={form.date_column} onChange={e => set({ date_column: e.target.value })}>
                      <option value="">— none —</option>
                      {columnKeys.map(k => <option key={k} value={k}>{k}</option>)}
                    </select>
                  </label>
                  <label>Date format in files (optional)
                    <input value={form.date_format} onChange={e => set({ date_format: e.target.value })} placeholder="%d-%m-%Y" />
                    <span className="ad-hint">Set this when files hold text dates like 01-08-2026, otherwise they can be read as 8 January.</span>
                  </label>
                  <label>Brand-restriction column (optional)
                    <select value={form.restrict_col} onChange={e => set({ restrict_col: e.target.value })}>
                      <option value="">— none —</option>
                      {columnKeys.map(k => <option key={k} value={k}>{k}</option>)}
                    </select>
                  </label>
                </div>

                <div className="ad-sub">Columns stamped on every inserted row (leave blank if the table has no such column)</div>
                <div className="ad-grid ad-grid3">
                  <label>File name column
                    <input value={form.audit_file_name} onChange={e => set({ audit_file_name: e.target.value })} placeholder="FILE_NAME" />
                  </label>
                  <label>Run date column
                    <input value={form.audit_run_date} onChange={e => set({ audit_run_date: e.target.value })} placeholder="RUN_DATE" />
                  </label>
                  <label>Load timestamp column
                    <input value={form.audit_load_run_date} onChange={e => set({ audit_load_run_date: e.target.value })} placeholder="LOAD_RUN_DATE" />
                  </label>
                </div>
              </div>

              <div className="ad-card">
                <div className="ad-card-head">Columns the user's file must contain</div>
                <div className="ad-muted">Keys must match the header names in the file and the Fabric column names. They are stored upper-case.</div>
                <table className="ad-cols">
                  <thead>
                    <tr><th>Key</th><th>Label</th><th>Number</th><th>Example</th><th>Description</th><th /></tr>
                  </thead>
                  <tbody>
                    {form.columns.map((c, i) => (
                      <tr key={i}>
                        <td><input value={c.key} onChange={e => setColumn(i, { key: e.target.value.toUpperCase() })} placeholder="STORE_CODE" /></td>
                        <td><input value={c.label} onChange={e => setColumn(i, { label: e.target.value })} placeholder="Store Code" /></td>
                        <td className="ad-center"><input type="checkbox" checked={c.numeric} onChange={e => setColumn(i, { numeric: e.target.checked })} /></td>
                        <td><input value={c.example} onChange={e => setColumn(i, { example: e.target.value })} /></td>
                        <td><input value={c.description} onChange={e => setColumn(i, { description: e.target.value })} /></td>
                        <td><button className="ad-x" title="Remove column" onClick={() => removeColumn(i)} disabled={form.columns.length === 1}>✕</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <button className="btn-outline" onClick={addColumn}>+ Add column</button>
              </div>

              <div className="ad-card">
                <div className="ad-card-head">Python logic for this portal</div>
                <div className="ad-hook">
                  <code>{hookFile}</code>
                  {existing?.has_hooks
                    ? <span className="ad-badge ad-badge-hook">File found — in use</span>
                    : <span className="ad-badge ad-badge-off">No file — built-in strategy only</span>}
                </div>
                <div className="ad-hint">
                  Custom Python is added through git, not typed here, so it gets reviewed before it can run.
                  Define <code>prepare(df, ctx)</code> to clean or check the uploaded file, and/or
                  <code> before_insert(cursor, ctx)</code> to replace the strategy's delete/truncate step.
                  See <code>backend/portals/_example_hooks.py</code>.
                </div>
              </div>

              {!isNew && (
                <div className="ad-card">
                  <div className="ad-card-head">Who can use this portal</div>
                  {access.length === 0 && <div className="ad-muted">No users yet. Admins can always upload.</div>}
                  {access.map(a => (
                    <div key={a.email} className="ad-access-row">
                      <span>{a.email}</span>
                      <span className="ad-muted">
                        {a.restrict_values?.length ? `Limited to: ${a.restrict_values.join(', ')}` : 'All values'}
                      </span>
                      <button className="ad-x" onClick={() => removeAccess(a.email)}>Remove</button>
                    </div>
                  ))}
                  <div className="ad-access-add">
                    <input value={newAccess.email} onChange={e => setNewAccess(n => ({ ...n, email: e.target.value }))}
                           placeholder="user@arvindfashions.com" />
                    <input value={newAccess.values} onChange={e => setNewAccess(n => ({ ...n, values: e.target.value }))}
                           placeholder={form.restrict_col ? `Allowed ${form.restrict_col} values, comma separated (blank = all)` : 'No restriction column set'}
                           disabled={!form.restrict_col} />
                    <button className="btn-outline" onClick={addAccess}>Add / update</button>
                  </div>
                </div>
              )}

              <div className="ad-actions">
                <button className="btn-primary" onClick={save} disabled={saving}>
                  {saving ? 'Saving…' : isNew ? 'Create portal' : 'Save changes'}
                </button>
              </div>
            </>
          )}
        </section>
      </main>
    </div>
  )
}
