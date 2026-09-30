// Minimal client-side routing helpers — no router dependency.
// URL shape: <base>/upload/<portal-id>  e.g. /uploadUI/upload/monthly-store-target-upload
// The leading segment is cosmetic; only the last path segment (the portal id) is read back.

export function basePath() {
  const base = import.meta.env.BASE_URL || '/'
  return base.endsWith('/') ? base : base + '/'
}

export function homePath() {
  return basePath()
}

export function portalPath(portal) {
  return `${basePath()}upload/${portal.id}`
}

/** Returns { portalId } | null based on the current URL. */
export function parseCurrentPath() {
  const base = basePath()
  const baseNoSlash = base.slice(0, -1)
  let path = window.location.pathname

  if (path === base || path === baseNoSlash) return null
  if (path.startsWith(base)) path = path.slice(base.length)
  else if (path.startsWith(baseNoSlash + '/')) path = path.slice(baseNoSlash.length + 1)

  path = path.replace(/^\/+|\/+$/g, '')
  if (!path) return null

  const parts = path.split('/').filter(Boolean)
  const portalId = parts[parts.length - 1]
  return portalId ? { portalId } : null
}

export function pushPortalPath(portal) {
  const url = portalPath(portal)
  if (window.location.pathname !== url) window.history.pushState({}, '', url)
}

export function pushHomePath() {
  const url = homePath()
  if (window.location.pathname !== url) window.history.pushState({}, '', url)
}
