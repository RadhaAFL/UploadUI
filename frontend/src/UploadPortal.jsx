import { useEffect, useMemo, useRef, useState } from 'react'
import { msalInstance } from './authConfig'
import arvindLogo from './assets/arvind-logo.png'
import { logEvent } from './logger'
import './UploadPortal.css'

const API = '/uploadportal-api'

function restrictionText(vals) {
  if (!vals) return ''
  if (Array.isArray(vals)) return vals.join(' · ')
  return Object.entries(vals)
    .filter(([, v]) => Array.isArray(v) && v.length > 0)
    .map(([k, v]) => `${k}: ${v.join(' / ')}`)
    .join(' · ')
}

export default function UploadPortal({ user, portal, onBack }) {
  const [template, setTemplate]   = useState(null)
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState('')
  const [validating, setValidating] = useState(false)
  const [staged, setStaged]       = useState(null)   // response from /upload/start
  const [committing, setCommitting] = useState(false)
  const [result, setResult]       = useState(null)    // response from /upload/commit
  const fileInputRef = useRef(null)

  const loadTemplate = () => {
    setLoading(true); setError('')
    fetch(`${API}/upload-template?portal_id=${encodeURIComponent(portal.id)}`)
      .then(r => r.json().then(d => ({ ok: r.ok, data: d })))
      .then(({ ok, data }) => {
        if (!ok) throw new Error(data.error || 'Could not load upload template')
        setTemplate(data)
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(loadTemplate, [portal.id])

  const reset = () => {
    setStaged(null); setResult(null); setError('')
    if (fileInputRef.current) fileInputRef.current.value = ''
    loadTemplate() // refresh period in case day rolled over
  }

  const handleFile = async (file) => {
    if (!file) return
    setValidating(true); setError(''); setStaged(null); setResult(null)
    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('portal_id', portal.id)
      formData.append('email', user.email)
      const res = await fetch(`${API}/upload/start`, { method: 'POST', body: formData })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || `Server error ${res.status}`)
      setStaged(data)
      logEvent(user, 'upload_validate', {
        portal_id: portal.id, portal_name: portal.name, filename: data.filename,
        row_count: data.row_count, period: data.period?.key,
      })
    } catch (e) {
      setError(e.message)
    } finally {
      setValidating(false)
    }
  }

  const handleCommit = async () => {
    if (!staged) return
    setCommitting(true); setError('')
    try {
      const res = await fetch(`${API}/upload/commit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ upload_id: staged.upload_id, email: user.email }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || `Server error ${res.status}`)
      setResult(data)
      setStaged(null)
      logEvent(user, 'upload_commit', {
        portal_id: portal.id, portal_name: portal.name,
        deleted: data.deleted, inserted: data.inserted, period: data.period?.key,
      })
    } catch (e) {
      setError(e.message)
    } finally {
      setCommitting(false)
    }
  }

  const columns = template?.columns || []
  const period = template?.period

  const nullWarnings = useMemo(() => {
    if (!staged?.null_counts) return []
    return Object.entries(staged.null_counts).filter(([, n]) => n > 0)
  }, [staged])

  return (
    <div className="up-page">
      <header className="kpi-header">
        <div className="kpi-header-left">
          <img src={arvindLogo} alt="Arvind Fashions" className="kpi-logo" />
          <button className="btn-back-portals" onClick={onBack}>Back to Portals</button>
          <div>
            <div className="kpi-title">{portal.name}</div>
            {template?.target_table && (
              <div className="kpi-subtitle">Uploads save to {template.target_table}</div>
            )}
          </div>
        </div>
        <div className="kpi-header-right">
          {restrictionText(portal.restrict_values) && (
            <span className="header-brand-badge">{restrictionText(portal.restrict_values)}</span>
          )}
          <span className="kpi-user">{user.displayName}</span>
          <button className="btn-signout" onClick={() => msalInstance.logoutRedirect()}>Sign out</button>
        </div>
      </header>

      <main className="up-main">
        {loading && <div className="kpi-state">Loading upload template...</div>}
        {error && <div className="error-bar"><strong>Error:</strong> {error}</div>}

        {!loading && template && (
          <>
            {period && (
              <section className="up-period-card">
                <div className="up-period-label">Expected upload period</div>
                <div className="up-period-value">{period.label}</div>
                <div className="up-period-hint">
                  Business dates in your file must fall between <strong>{period.start}</strong> and <strong>{period.end}</strong>.
                  {' '}Uploads on or after day {period.cutover_day} of the month count toward next month.
                </div>
              </section>
            )}

            <section className="up-columns-card">
              <div className="up-columns-head">Required columns</div>
              <div className="up-columns-list">
                {columns.map(c => (
                  <span key={c.key} className={`up-col-chip ${c.numeric ? 'up-col-numeric' : ''}`}>
                    {c.label || c.key}
                  </span>
                ))}
              </div>
            </section>

            {!result && (
              <section className="up-dropzone-card">
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".xlsx,.xlsm,.csv"
                  onChange={e => handleFile(e.target.files?.[0])}
                  disabled={validating || committing}
                  className="up-file-input"
                  id="up-file-input"
                />
                <label htmlFor="up-file-input" className="up-file-label">
                  {validating ? (
                    <><span className="spinner" /> Validating file…</>
                  ) : (
                    <>📤 Choose a .xlsx or .csv file matching the template above</>
                  )}
                </label>
              </section>
            )}

            {staged && !result && (
              <section className="up-preview-card">
                <div className="up-preview-head">
                  <div>
                    <div className="up-preview-file">{staged.filename}</div>
                    <div className="up-preview-meta">
                      {staged.row_count.toLocaleString('en-IN')} rows
                      {staged.brands_in_file?.length > 0 && <> · {staged.brands_in_file.join(', ')}</>}
                      {staged.period && <> · {staged.period.label}</>}
                    </div>
                  </div>
                  <button className="btn-outline" onClick={reset} disabled={committing}>Choose a different file</button>
                </div>

                {nullWarnings.length > 0 && (
                  <div className="up-warning">
                    ⚠ Some numeric values are blank: {nullWarnings.map(([k, n]) => `${k} (${n})`).join(', ')}
                  </div>
                )}

                <div className="up-preview-table-wrap">
                  <table className="kpi-table">
                    <thead>
                      <tr>{columns.map(c => <th key={c.key}>{c.label || c.key}</th>)}</tr>
                    </thead>
                    <tbody>
                      {staged.preview.map((row, i) => (
                        <tr key={i}>
                          {columns.map(c => <td key={c.key}>{row[c.key] ?? '—'}</td>)}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="up-preview-note">Showing first {staged.preview.length} of {staged.row_count.toLocaleString('en-IN')} rows.</div>
                </div>

                <div className="up-confirm-bar">
                  <div className="up-confirm-text">
                    This will replace existing {staged.period?.label || 'matching'} data
                    {staged.brands_in_file?.length > 0 && <> for {staged.brands_in_file.join(', ')}</>} with the {staged.row_count.toLocaleString('en-IN')} rows above.
                  </div>
                  <button className="btn-primary" onClick={handleCommit} disabled={committing}>
                    {committing ? (<><span className="spinner" /> Uploading…</>) : 'Confirm & Upload'}
                  </button>
                </div>
              </section>
            )}

            {result && (
              <section className="up-result-card">
                <div className="up-result-icon">✅</div>
                <div className="up-result-title">Upload complete</div>
                <div className="up-result-body">
                  <strong>{result.inserted.toLocaleString('en-IN')}</strong> rows inserted
                  {result.deleted > 0 && <> · <strong>{result.deleted.toLocaleString('en-IN')}</strong> existing rows replaced</>}
                  {' '}for <strong>{result.period?.label}</strong>
                  {result.brands?.length > 0 && <> ({result.brands.join(', ')})</>}.
                  {' '}Verified <strong>{result.verified.toLocaleString('en-IN')}</strong> rows in {result.table}.
                </div>
                <button className="btn-primary" onClick={reset}>Upload another file</button>
              </section>
            )}
          </>
        )}
      </main>
    </div>
  )
}
