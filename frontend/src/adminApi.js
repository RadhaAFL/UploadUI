const API = '/uploadportal-api'

// Admin endpoints identify the caller with `caller_email` (see _require_admin
// in app.py): query string for GET/DELETE, JSON body for POST/PUT.
export async function adminApi(user, path, { method = 'GET', body } = {}) {
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

// The backend stores UTC as 'YYYY-MM-DD HH:MM:SS'; show it in the viewer's time.
export function formatTime(ts) {
  const d = new Date(String(ts).replace(' ', 'T') + 'Z')
  return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleString('en-IN', { hour12: false })
}
