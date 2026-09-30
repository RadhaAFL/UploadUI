import { useEffect, useState } from 'react'
import { useMsal, useIsAuthenticated } from '@azure/msal-react'
import { loginRequest, msalInstance } from './authConfig'
import { logEvent } from './logger'
import UploadPortalHome from './UploadPortalHome'
import UploadPortal from './UploadPortal'
import arvindLogo from './assets/arvind-logo.png'
import { parseCurrentPath, pushHomePath, pushPortalPath } from './routing'

const API = '/uploadportal-api'

// ── Microsoft logo (no external dependency) ──────────────────────────────
function MicrosoftIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 21 21" aria-hidden="true" style={{ flexShrink: 0 }}>
      <rect x="1"  y="1"  width="9" height="9" fill="#f25022"/>
      <rect x="11" y="1"  width="9" height="9" fill="#7fba00"/>
      <rect x="1"  y="11" width="9" height="9" fill="#00a4ef"/>
      <rect x="11" y="11" width="9" height="9" fill="#ffb900"/>
    </svg>
  )
}

function LoginPage({ onLogin, loading }) {
  return (
    <div className="auth-bg">
      <div className="auth-split">
        <div className="auth-panel-left">
          <div className="auth-brand">
            <div className="auth-logo-wrap"><img src={arvindLogo} alt="Arvind Fashions" className="auth-logo-img" /></div>
            <h1>Upload Portal</h1>
            <p>Get data into Arvind Analytics, the right way, every time</p>
          </div>
          <div className="auth-tagline">
            <div className="auth-feature">📤 Admin-defined upload templates</div>
            <div className="auth-feature">🗓️ Automatic period rules per template</div>
            <div className="auth-feature">🔒 Brand-restricted access</div>
          </div>
        </div>
        <div className="auth-panel-right">
          <div className="auth-card">
            <div className="auth-card-logo"><img src={arvindLogo} alt="Arvind Fashions" className="auth-card-logo-img" /></div>
            <h2 className="auth-title">Welcome back</h2>
            <p className="auth-subtitle">Sign in with your Arvind Fashions Microsoft account to continue.</p>
            <button className="ms-btn" onClick={onLogin} disabled={loading}>
              {loading ? <span className="auth-spinner" /> : <MicrosoftIcon />}
              {loading ? 'Redirecting…' : 'Sign in with Microsoft'}
            </button>
            <p className="auth-footer">By signing in you agree to Arvind's data access policy.</p>
          </div>
        </div>
      </div>
    </div>
  )
}

function AccessDenied({ email }) {
  return (
    <div className="auth-bg">
      <div className="auth-center-card">
        <div className="auth-denied-icon">🚫</div>
        <h2>Access Denied</h2>
        <p><strong>{email}</strong> is not authorised to use the Upload Portal.</p>
        <p className="auth-denied-hint">Contact your administrator to request access.</p>
        <button className="ms-btn ms-btn-outline" onClick={() => msalInstance.logoutRedirect()}>Sign out</button>
      </div>
    </div>
  )
}

export default function AuthWrapper() {
  const { accounts } = useMsal()
  const isAuthenticated = useIsAuthenticated()
  const [signing, setSigning]   = useState(false)
  const [access, setAccess]     = useState(undefined) // undefined = loading
  const [activePortal, setActivePortal] = useState(null)
  const [deepLink] = useState(() => parseCurrentPath())

  useEffect(() => {
    if (!isAuthenticated || !accounts.length) {
      setAccess(undefined)
      return
    }
    const account = accounts[0]
    const email = account.username
    fetch(`${API}/check-access?email=${encodeURIComponent(email)}`)
      .then(r => r.json())
      .then(data => {
        const user = {
          email,
          displayName: account.name ?? email,
          isAdmin: !!data.is_admin,
        }
        setAccess(data.allowed ? user : null)
        logEvent(user, 'login', { allowed: data.allowed, is_admin: data.is_admin })
      })
      .catch(() => setAccess(null))
  }, [isAuthenticated, accounts])

  useEffect(() => {
    const onPop = () => window.location.reload()
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  if (!isAuthenticated) {
    return (
      <LoginPage
        loading={signing}
        onLogin={() => {
          setSigning(true)
          msalInstance.loginRedirect(loginRequest).catch(() => setSigning(false))
        }}
      />
    )
  }

  if (access === undefined) return null
  if (access === null) return <AccessDenied email={accounts[0]?.username || ''} />

  if (!activePortal) {
    return (
      <UploadPortalHome
        user={access}
        deepLinkPortalId={deepLink?.portalId || null}
        onSelect={portal => { setActivePortal(portal); pushPortalPath(portal) }}
        onSignOut={() => msalInstance.logoutRedirect()}
      />
    )
  }

  return (
    <UploadPortal
      user={access}
      portal={activePortal}
      onBack={() => { setActivePortal(null); pushHomePath() }}
    />
  )
}
