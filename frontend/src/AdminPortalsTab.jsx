import { useMemo, useState } from 'react'
import { adminApi } from './adminApi'

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

export default function AdminPortalsTab({ user, portals, strategies, loading, onChanged, onManageUsers }) {
  const [selected, setSelected] = useState(null)   // portal id | 'new' | null
  const [form, setForm]         = useState(null)
  const [saving, setSaving]     = useState(false)
  const [error, setError]       = useState('')
  const [notice, setNotice]     = useState('')

  const selectPortal = p => { setError(''); setNotice(''); setSelected(p.id); setForm(formFromPortal(p)) }
  const startNew = () => { setError(''); setNotice(''); setSelected('new'); setForm(emptyForm()) }

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
        await onChanged()
        setSelected(form.id.trim())
        setNotice('Portal created. Add the users who may use it in the Users tab.')
      } else {
        await adminApi(user, `/admin/portals/${encodeURIComponent(form.id)}`, {
          method: 'PUT',
          body: { name: form.name, description: form.description, config, is_active: form.is_active },
        })
        await onChanged()
        setNotice('Saved.')
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  const hookFile = existing?.hook_file || `backend/portals/${form?.id || '<portal-id>'}.py`

  return (
    <div className="ad-layout">
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
      </aside>

      <section className="ad-editor">
        {error && <div className="error-bar"><strong>Error:</strong> {error}</div>}
        {notice && (
          <div className="ad-notice">
            {notice}
            {existing && <button className="ad-linkbtn" onClick={() => onManageUsers(existing.id)}>Manage users →</button>}
          </div>
        )}

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

            <div className="ad-actions">
              {!isNew && <button className="btn-outline" onClick={() => onManageUsers(form.id)}>Manage users →</button>}
              <button className="btn-primary" onClick={save} disabled={saving}>
                {saving ? 'Saving…' : isNew ? 'Create portal' : 'Save changes'}
              </button>
            </div>
          </>
        )}
      </section>
    </div>
  )
}
