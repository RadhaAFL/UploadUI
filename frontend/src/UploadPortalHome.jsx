import { useEffect, useState } from 'react'
import arvindLogo from './assets/arvind-logo.png'

export default function UploadPortalHome({ user, onSelect, onSignOut, deepLinkPortalId }) {
  const [portals, setPortals] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState(null)

  useEffect(() => {
    fetch(`/uploadportal-api/my-portals?email=${encodeURIComponent(user.email)}`)
      .then(r => r.json())
      .then(data => {
        if (data.error) throw new Error(data.error)
        setPortals(data.portals || [])
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [user.email])

  // Auto-open the portal named in the URL (e.g. /uploadUI/upload/<id>) once
  // the accessible-portals list has loaded. A stale/unknown id just leaves
  // the list showing — harmless.
  useEffect(() => {
    if (!deepLinkPortalId || loading) return
    const match = portals.find(p => p.id === deepLinkPortalId)
    if (match) onSelect(match)
  }, [deepLinkPortalId, portals, loading, onSelect])

  return (
    <div className="ph-bg">
      <header className="ph-header">
        <div className="ph-logo"><img src={arvindLogo} alt="Arvind Fashions" className="ph-logo-img" /></div>
        <div className="ph-header-title">Upload Portal</div>
        <div className="ph-header-right">
          <span className="ph-user">{user.displayName}</span>
          <button className="btn-signout" onClick={onSignOut}>Sign out</button>
        </div>
      </header>

      <main className="ph-main">
        <div className="ph-hero">
          <h1 className="ph-hero-title">Upload Templates</h1>
          <p className="ph-hero-sub">Select a template to upload data in the required format</p>
        </div>

        {loading && (
          <div className="ph-grid">
            {[1, 2].map(i => <div key={i} className="ph-card ph-card-skeleton" />)}
          </div>
        )}

        {error && <div className="ph-error">⚠ Could not load templates: {error}</div>}

        {!loading && !error && portals.length === 0 && (
          <div className="ph-empty">
            <div className="ph-empty-icon">🔒</div>
            <p>You don't have access to any upload templates yet.</p>
            <p className="ph-empty-hint">Contact your administrator to request access.</p>
          </div>
        )}

        {!loading && !error && portals.length > 0 && (
          <div className="ph-grid">
            {portals.map(portal => (
              <div key={portal.id} className="ph-card" onClick={() => onSelect(portal)}>
                <div className="ph-card-icon">📤</div>
                <div className="ph-card-body">
                  <div className="ph-card-name">{portal.name}</div>
                  {portal.description && <div className="ph-card-desc">{portal.description}</div>}
                  <div className="ph-card-meta">
                    <span className="ph-card-view">{portal.config?.target_table}</span>
                  </div>
                </div>
                <div className="ph-card-arrow">→</div>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
