import { useCallback, useEffect, useState } from 'react'
import { msalInstance } from './authConfig'
import arvindLogo from './assets/arvind-logo.png'
import { adminApi } from './adminApi'
import AdminUsersTab from './AdminUsersTab'
import AdminAdminsTab from './AdminAdminsTab'
import AdminPortalsTab from './AdminPortalsTab'
import AdminLogTab from './AdminLogTab'
import './AdminConsole.css'

const TABS = [
  { id: 'users',   label: 'Users',        icon: '👥' },
  { id: 'admins',  label: 'Admins',       icon: '👑' },
  { id: 'portals', label: 'Portals',      icon: '📁' },
  { id: 'log',     label: 'Activity Log', icon: '📋' },
]

export default function AdminConsole({ user, onBack }) {
  const [tab, setTab]               = useState('users')
  const [portals, setPortals]       = useState([])
  const [admins, setAdmins]         = useState([])
  const [strategies, setStrategies] = useState([])
  const [loading, setLoading]       = useState(true)
  const [error, setError]           = useState('')
  const [usersPortalId, setUsersPortalId] = useState(null)

  const loadPortals = useCallback(async () => {
    const data = await adminApi(user, '/admin/portals')
    setPortals(data.portals || [])
    return data.portals || []
  }, [user])

  const loadAdmins = useCallback(async () => {
    const data = await adminApi(user, '/admin/admins')
    setAdmins(data.admins || [])
  }, [user])

  useEffect(() => {
    Promise.all([adminApi(user, '/admin/strategies'), loadPortals(), loadAdmins()])
      .then(([s, list]) => {
        setStrategies(s.strategies || [])
        setUsersPortalId(id => id || list[0]?.id || null)
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [user, loadPortals, loadAdmins])

  const manageUsers = id => { setUsersPortalId(id); setTab('users') }

  return (
    <div className="up-page">
      <header className="kpi-header">
        <div className="kpi-header-left">
          <img src={arvindLogo} alt="Arvind Fashions" className="kpi-logo" />
          <button className="btn-back-portals" onClick={onBack}>Back to Portals</button>
          <div>
            <div className="kpi-title">Admin console</div>
            <div className="kpi-subtitle">Users, administrators, portals and activity</div>
          </div>
        </div>
        <div className="kpi-header-right">
          <span className="kpi-user">{user.displayName}</span>
          <button className="btn-signout" onClick={() => msalInstance.logoutRedirect()}>Sign out</button>
        </div>
      </header>

      <main className="ad-main">
        <nav className="ad-tabs" role="tablist">
          {TABS.map(t => (
            <button key={t.id} role="tab" aria-selected={tab === t.id}
                    className={`ad-tab ${tab === t.id ? 'ad-tab-active' : ''}`} onClick={() => setTab(t.id)}>
              <span aria-hidden="true">{t.icon}</span> {t.label}
              {t.id === 'admins' && <span className="ad-tab-count">{admins.length}</span>}
            </button>
          ))}
        </nav>

        {error && <div className="error-bar"><strong>Error:</strong> {error}</div>}

        {tab === 'users' && (
          <AdminUsersTab user={user} portals={portals} portalId={usersPortalId}
                         setPortalId={setUsersPortalId} onChanged={loadPortals} />
        )}
        {tab === 'admins' && <AdminAdminsTab user={user} admins={admins} onChanged={loadAdmins} />}
        {tab === 'portals' && (
          <AdminPortalsTab user={user} portals={portals} strategies={strategies} loading={loading}
                           onChanged={loadPortals} onManageUsers={manageUsers} />
        )}
        {tab === 'log' && <AdminLogTab user={user} />}
      </main>
    </div>
  )
}
