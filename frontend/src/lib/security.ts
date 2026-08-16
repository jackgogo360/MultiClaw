import { API_BASE } from "./constants";

let csrfToken: string | null = null;
let csrfTokenRequest: Promise<string> | null = null;

async function fetchCsrfToken() {
  const response = await fetch(`${API_BASE}/auth/csrf`, {
    credentials: "include",
  });

  if (!response.ok) {
    throw new Error("Failed to refresh CSRF token.");
  }

  const payload = (await response.json()) as { token?: unknown };
  if (typeof payload.token !== "string" || payload.token.length === 0) {
    throw new Error("CSRF token response was invalid.");
  }

  csrfToken = payload.token;
  return payload.token;
}

export async function ensureCsrfToken() {
  if (csrfToken) {
    return csrfToken;
  }
  if (!csrfTokenRequest) {
    csrfTokenRequest = fetchCsrfToken().finally(() => {
      csrfTokenRequest = null;
    });
  }
  return csrfTokenRequest;
}

export async function invalidateCsrfToken() {
  csrfToken = null;
  return ensureCsrfToken();
}

export function clearCsrfToken() {
  csrfToken = null;
  csrfTokenRequest = null;
}
