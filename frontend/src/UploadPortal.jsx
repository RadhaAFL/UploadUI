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

// What an upload will do to the target table, in the words a user needs
// before pressing confirm. `strategy` comes from the portal's load_strategy.
function loadRuleText(strategy, table, period, brands) {
  if (strategy === 'truncate_load') return `This replaces ALL existing data in ${table} with your file.`
  if (strategy === 'append') return `This adds your rows to the existing data in ${table}. Nothing is deleted.`
  if (strategy === 'upsert') return `This updates rows in ${table} that match your file, and adds the rest. Other rows are not touched.`
  const brandPart = brands?.length ? ` for ${brands.join(', ')}` : ''
  return `This replaces existing ${period?.label || 'matching'} data${brandPart} with your file.`
}

// Wording for the background upload's progress and outcome.
function progressTitle(job) {
  if (job.state === 'publishing' && job.mode === 'staged') return 'Applying your file to the live table…'
  if (job.state === 'staging') return 'Loading your file into a staging area…'
  if (job.state === 'starting') return 'Starting your upload…'
  return 'Loading your file…'
}

function progressPercent(job) {
  if (!job.rows_total) return 0
  if (job.state === 'publishing' && job.mode === 'staged') return 100
  return Math.min(100, Math.round(((job.rows_done || 0) / job.rows_total) * 100))
}

// What a user must know after a failure: is the live table safe?
function liveChangedText(liveChanged) {
  if (liveChanged === 'no') return 'Nothing in the live table was changed, so it is safe to try again.'
  if (liveChanged === 'yes') return 'The live table may now hold only part of this file. Upload the file again to complete it.'
  return "We couldn't confirm what happened to the live table. Please check it before uploading again."
}

export default function UploadPortal({ user, portal, onBack }) {
  const [template, setTemplate]   = useState(null)
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState('')
  const [validating, setValidating] = useState(false)
  const [staged, setStaged]       = useState(null)   // response from /upload/start
  const [committing, setCommitting] = useState(false)   // the request that starts the job
  const [job, setJob]             = useState(null)    // the background upload: running, done or failed
  const [linkLost, setLinkLost]   = useState(false)   // can't reach the server while polling
  const fileInputRef = useRef(null)

  const statusUrl = id => `${API}/upload/status?job_id=${encodeURIComponent(id)}&email=${encodeURIComponent(user.email)}`

  const loadTemplate = () => {
    setLoading(true); setError('')
    fetch(`${API}/upload-template?portal_id=${encodeURIComponent(portal.id)}&email=${encodeURIComponent(user.email)}`)
      .then(r => r.json().then(d => ({ ok: r.ok, data: d })))
      .then(({ ok, data }) => {
        if (!ok) throw new Error(data.error || 'Could not load upload template')
        setTemplate(data)
        // The user closed the page while their upload was running: pick it back up.
        if (data.busy?.mine) {
          fetch(statusUrl(data.busy.job_id)).then(r => r.json()).then(j => { if (j.job_id) setJob(j) }).catch(() => {})
        }
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(loadTemplate, [portal.id])

  // While an upload runs on the server, ask how it is going. The load does not
  // depend on this page: if the connection drops or the tab closes it carries on.
  useEffect(() => {
    if (!job || job.finished) return undefined
    let cancelled = false
    const tick = async () => {
      try {
        const res = await fetch(statusUrl(job.job_id))
        const data = await res.json()
        if (cancelled) return
        if (!res.ok) throw new Error(data.error || `Server error ${res.status}`)
        setJob(data); setLinkLost(false)
      } catch {
        if (!cancelled) setLinkLost(true)
      }
    }
    const timer = setInterval(tick, 1500)
    return () => { cancelled = true; clearInterval(timer) }
  }, [job?.job_id, job?.finished, user.email])

  const reset = () => {
    setStaged(null); setJob(null); setLinkLost(false); setError('')
    if (fileInputRef.current) fileInputRef.current.value = ''
    loadTemplate() // refresh period in case day rolled over
  }

  const handleFile = async (file) => {
    if (!file) return
    setValidating(true); setError(''); setStaged(null); setJob(null)
    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('portal_id', portal.id)
      formData.append('email', user.email)
      const res = await fetch(`${API}/upload/start`, { method: 'POST', body: formData })
      const data = await res.json()
      if (!res.ok) {
        if (res.status === 409) loadTemplate()   // someone else is uploading: show the banner too
        throw new Error(data.error || `Server error ${res.status}`)
      }
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

  // Starts the upload on the server and returns straight away; the page then
  // follows its progress. (The server records the commit itself, so nothing is
  // logged from here.)
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
      if (res.status === 409) loadTemplate()   // portal busy: your validated file is kept, press Confirm again later
      if (!res.ok) throw new Error(data.error || `Server error ${res.status}`)
      setJob({ job_id: data.job_id, state: 'starting', mode: data.mode, rows_total: data.rows_total, rows_done: 0, finished: false })
      setStaged(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setCommitting(false)
    }
  }

  const running = !!job && !job.finished
  const busyByOther = !!template?.busy && !template.busy.mine && !job

  const columns = template?.columns || []
  const period = template?.period
  const strategy = template?.load_strategy || 'replace_period'
  const sampleUrl = format =>
    `${API}/portals/${encodeURIComponent(portal.id)}/sample-template?format=${format}`

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

            <section className={`up-rule-card ${strategy === 'truncate_load' ? 'up-rule-warn' : ''}`}>
              <div className="up-rule-head">What this upload does</div>
              <div className="up-rule-body">
                {loadRuleText(strategy, template.target_table, period, null)}
              </div>
            </section>

            <section className="up-columns-card">
              <div className="up-columns-top">
                <div className="up-columns-head">Required columns</div>
                <div className="up-sample-links">
                  <span className="up-sample-label">Not sure of the format? Download a sample template:</span>
                  <a className="btn-outline up-sample-btn" href={sampleUrl('xlsx')} download>Excel (.xlsx)</a>
                  <a className="btn-outline up-sample-btn" href={sampleUrl('csv')} download>CSV</a>
                </div>
              </div>
              <div className="up-columns-list">
                {columns.map(c => (
                  <span key={c.key} className={`up-col-chip ${c.numeric ? 'up-col-numeric' : ''}`}>
                    {c.label || c.key}
                  </span>
                ))}
              </div>
            </section>

            {busyByOther && (
              <section className="up-busy-card">
                <div className="up-busy-title">⏳ Someone else is uploading to this portal</div>
                <div className="up-busy-body">{template.busy.message}</div>
                <button className="btn-outline" onClick={loadTemplate}>Check again</button>
              </section>
            )}

            {!job && !busyByOther && (
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

            {staged && !job && (
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
                    {loadRuleText(staged.load_strategy || strategy, template.target_table, staged.period, staged.brands_in_file)}
                    {' '}{staged.row_count.toLocaleString('en-IN')} rows will be loaded.
                  </div>
                  <button className="btn-primary" onClick={handleCommit} disabled={committing}>
                    {committing ? (<><span className="spinner" /> Uploading…</>) : 'Confirm & Upload'}
                  </button>
                </div>
              </section>
            )}

            {running && (
              <section className="up-progress-card">
                <div className="up-progress-title"><span className="spinner up-spinner-dark" /> {progressTitle(job)}</div>
                <div className="up-progress-bar" aria-label="Upload progress">
                  <div className="up-progress-fill" style={{ width: `${progressPercent(job)}%` }} />
                </div>
                <div className="up-progress-meta">
                  {job.rows_total ? `${(job.rows_done || 0).toLocaleString('en-IN')} of ${job.rows_total.toLocaleString('en-IN')} rows` : ''}
                </div>
                <div className="up-progress-note">
                  {job.mode === 'staged'
                    ? "Your live data is not changed until the whole file has loaded and checked out; then it is swapped in all at once."
                    : "Please don't upload this file again while it is running."}
                  {' '}You can close this page — the upload keeps running on the server, and you'll see the result when you come back.
                </div>
                {linkLost && (
                  <div className="up-warning">
                    Can't reach the server right now. Your upload is still running — trying again…
                  </div>
                )}
              </section>
            )}

            {job?.state === 'done' && (
              <section className="up-result-card">
                <div className="up-result-icon">✅</div>
                <div className="up-result-title">Upload complete</div>
                <div className="up-result-body">
                  <strong>{(job.inserted ?? 0).toLocaleString('en-IN')}</strong> rows inserted
                  {job.deleted > 0 && <> · <strong>{job.deleted.toLocaleString('en-IN')}</strong> existing rows {job.load_strategy === 'truncate_load' ? 'removed' : 'replaced'}</>}
                  {job.period_label && <> for <strong>{job.period_label}</strong></>}
                  {job.brands?.length > 0 && <> ({job.brands.join(', ')})</>}.
                  {job.verified != null && <>{' '}Verified <strong>{job.verified.toLocaleString('en-IN')}</strong> rows in {job.table || template.target_table}.</>}
                </div>
                {job.mode === 'staged' && (
                  <div className="up-result-sub">Checked in a staging area first, then swapped into the live table in one step.</div>
                )}
                <button className="btn-primary" onClick={reset}>Upload another file</button>
              </section>
            )}

            {job?.state === 'failed' && (
              <section className="up-failed-card">
                <div className="up-result-icon">⚠️</div>
                <div className="up-result-title">Upload did not complete</div>
                <div className="up-result-body">{job.message}</div>
                <div className={`up-failed-state up-failed-${job.live_changed || 'unknown'}`}>{liveChangedText(job.live_changed)}</div>
                <button className="btn-primary" onClick={reset}>Start again</button>
              </section>
            )}
          </>
        )}
      </main>
    </div>
  )
}
