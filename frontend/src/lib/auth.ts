/**
 * Sessione utente lato client (PRD §2.1, §13.1).
 *
 * Il backend ora richiede un access token su tutte le rotte di dominio:
 * qui vivono il salvataggio della sessione (localStorage, app 100% locale
 * senza cookie di terzi), il login/logout e il refresh single-flight del
 * token. `authFetch` in `api.ts` usa questi helper per allegare
 * l'Authorization header e rinnovare il token su 401.
 */

const KEY_ACCESS = "trans.access_token";
const KEY_REFRESH = "trans.refresh_token";
const KEY_ROLE = "trans.role";
const KEY_EMAIL = "trans.email";

const API = "/api/v1";

export interface TokenPairResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(KEY_ACCESS);
}

export function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(KEY_REFRESH);
}

export function getRole(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(KEY_ROLE);
}

export function getEmail(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(KEY_EMAIL);
}

export function isLoggedIn(): boolean {
  return Boolean(getAccessToken());
}

export function saveSession(
  pair: TokenPairResponse,
  meta?: { email?: string; role?: string }
): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(KEY_ACCESS, pair.access_token);
  window.localStorage.setItem(KEY_REFRESH, pair.refresh_token);
  if (meta?.email) window.localStorage.setItem(KEY_EMAIL, meta.email);
  if (meta?.role) window.localStorage.setItem(KEY_ROLE, meta.role);
}

export function clearSession(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(KEY_ACCESS);
  window.localStorage.removeItem(KEY_REFRESH);
  window.localStorage.removeItem(KEY_ROLE);
  window.localStorage.removeItem(KEY_EMAIL);
}

/** POST /auth/login: rotta aperta, nessun token richiesto. */
export async function loginRequest(
  email: string,
  password: string
): Promise<{ ok: boolean; detail?: string }> {
  const res = await fetch(`${API}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* corpo non JSON */
    }
    return { ok: false, detail };
  }
  const pair = (await res.json()) as TokenPairResponse;
  saveSession(pair, { email });
  return { ok: true };
}

let refreshing: Promise<boolean> | null = null;

/**
 * Rinnova l'access token col refresh token (single-flight: richieste
 * concorrenti condividono la stessa promessa). `false` = sessione scaduta.
 */
export function refreshAccessToken(): Promise<boolean> {
  if (typeof window === "undefined") return Promise.resolve(false);
  if (!refreshing) {
    refreshing = (async () => {
      const token = getRefreshToken();
      if (!token) {
        clearSession();
        return false;
      }
      try {
        const res = await fetch(`${API}/auth/refresh`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) {
          clearSession();
          return false;
        }
        const pair = (await res.json()) as TokenPairResponse;
        saveSession(pair);
        return true;
      } catch {
        return false;
      } finally {
        refreshing = null;
      }
    })();
  }
  return refreshing;
}

/** Logout: best-effort (audit lato backend) poi cancella la sessione. */
export async function logoutRequest(): Promise<void> {
  const token = getAccessToken();
  try {
    if (token) {
      await fetch(`${API}/auth/logout`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
    }
  } catch {
    /* il logout locale non deve mai bloccarsi */
  }
  clearSession();
}
