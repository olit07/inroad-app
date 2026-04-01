/**
 * static/auth.js
 * Shared auth helpers for all inroad pages.
 *
 * Usage:
 *   <script src="/static/auth.js"></script>
 *
 * Then call:
 *   setAccessToken(token)    — store token after login
 *   getAccessToken()         — read token (in-memory only, never localStorage)
 *   apiFetch(url, options)   — drop-in fetch that adds Bearer header,
 *                              auto-refreshes on 401, and redirects to /login
 *                              if refresh also fails
 */

// ── In-memory token storage ──────────────────────────────────────────────────
// Never written to localStorage or sessionStorage for security.

let _accessToken = null;

function setAccessToken(token) {
  _accessToken = token;
}

function getAccessToken() {
  return _accessToken;
}

// ── API base URL ─────────────────────────────────────────────────────────────

const _API_BASE =
  window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
    ? `${window.location.protocol}//${window.location.hostname}:5001`
    : '';

// ── apiFetch ─────────────────────────────────────────────────────────────────

/**
 * Drop-in fetch replacement that:
 *   1. Adds Authorization: Bearer <token> if we have a token in memory
 *   2. On 401, tries POST /auth/refresh to get a new access token
 *   3. Retries the original request once with the new token
 *   4. If refresh also fails, redirects to /login
 */
async function apiFetch(url, options = {}) {
  // Resolve relative URLs against the API base
  const fullUrl = url.startsWith('/') ? _API_BASE + url : url;

  const makeHeaders = () => {
    const headers = Object.assign({}, options.headers || {});
    if (_accessToken) {
      headers['Authorization'] = `Bearer ${_accessToken}`;
    }
    return headers;
  };

  // First attempt
  let resp = await fetch(fullUrl, {
    ...options,
    credentials: 'include',   // needed for the refresh-token cookie
    headers: makeHeaders(),
  });

  if (resp.status !== 401) {
    return resp;
  }

  // 401 — try to refresh
  const refreshed = await _tryRefresh();
  if (!refreshed) {
    // Refresh failed — send the user to login
    window.location.href = '/login';
    // Return a synthetic response so callers don't throw
    return new Response(JSON.stringify({ error: 'unauthenticated' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // Retry original request with the new token
  resp = await fetch(fullUrl, {
    ...options,
    credentials: 'include',
    headers: makeHeaders(),
  });
  return resp;
}

// ── Internal helpers ─────────────────────────────────────────────────────────

async function _tryRefresh() {
  try {
    const resp = await fetch(`${_API_BASE}/auth/refresh`, {
      method: 'POST',
      credentials: 'include',
    });
    if (!resp.ok) return false;
    const data = await resp.json();
    if (data.access_token) {
      setAccessToken(data.access_token);
      return true;
    }
    return false;
  } catch (_) {
    return false;
  }
}

// ── Convenience: logout ──────────────────────────────────────────────────────

async function authLogout() {
  try {
    await fetch(`${_API_BASE}/auth/logout`, {
      method: 'POST',
      credentials: 'include',
    });
  } catch (_) {}
  setAccessToken(null);
  window.location.href = '/login';
}
