import { useState } from 'react'
import { adminApi } from './adminApi'

export default function AdminAdminsTab({ user, admins, onChanged }) {
  const [newAdmin, setNewAdmin] = useState('')
  const [error, setError]       = useState('')
  const [notice, setNotice]     = useState('')
  const me = user.email.toLowerCase()

  const add = async () => {
    const email = newAdmin.trim().toLowerCase()
    if (!email) return
    setError(''); setNotice('')
    try {
      await adminApi(user, '/admin/admins', { method: 'POST', body: { email } })
      setNewAdmin('')
      setNotice(`${email} is now an administrator.`)
      await onChanged()
    } catch (e) { setError(e.message) }
  }

  const remove = async email => {
    if (!window.confirm(`Remove admin access for ${email}? They will still be able to use any portals they've been given access to.`)) return
    setError(''); setNotice('')
    try {
      await adminApi(user, `/admin/admins?email=${encodeURIComponent(email)}`, { method: 'DELETE' })
      await onChanged()
    } catch (e) { setError(e.message) }
  }

  return (
    <div className="ad-tabpane">
      <div className="ad-card">
        <div className="ad-card-head">Administrators</div>
        <div className="ad-muted">
          Admins can create portals, change load rules, manage users and other admins, and upload to every portal.
        </div>
      </div>

      {error && <div className="error-bar"><strong>Error:</strong> {error}</div>}
      {notice && <div className="ad-notice">{notice}</div>}

      <div className="ad-card ad-tablecard">
        <table className="ad-table">
          <thead><tr><th>Email</th><th className="ad-right">Actions</th></tr></thead>
          <tbody>
            {admins.map(a => (
              <tr key={a}>
                <td>{a}{a === me && <span className="ad-you"> (you)</span>}</td>
                <td className="ad-right">
                  <button className="ad-btn-sm ad-btn-danger" disabled={a === me}
                          title={a === me ? "You can't remove your own access" : `Remove ${a}`}
                          onClick={() => remove(a)}>Remove</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="ad-card ad-addcard">
        <div className="ad-card-head">Add administrator</div>
        <div className="ad-adminadd">
          <input value={newAdmin} onChange={e => setNewAdmin(e.target.value)}
                 onKeyDown={e => { if (e.key === 'Enter') add() }}
                 placeholder="name@arvindfashions.com" />
          <button className="btn-primary" onClick={add} disabled={!newAdmin.trim()}>Add admin</button>
        </div>
      </div>
    </div>
  )
}
