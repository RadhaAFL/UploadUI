/**
 * Fire-and-forget audit logger.
 * Never throws or blocks the UI — all errors are silently swallowed.
 */
const API = '/uploadportal-api/logs'

export function logEvent(user, action, details = {}) {
  fetch(API, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      email:   user?.email   || '',
      name:    user?.displayName || '',
      action,
      details,
    }),
  }).catch(() => {})
}
