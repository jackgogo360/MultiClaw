import { API_BASE } from "./constants";

export interface Session {
  id: string;
  title: string;
  status: "active" | "archived";
  created_at: string;
  updated_at: string;
}

export interface Message {
  role: "user" | "assistant";
  content: string;
  created_at?: string;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
  });
  if (res.status === 401) {
    throw new AuthError();
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Request failed" }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

export class AuthError extends Error {
  constructor() {
    super("Unauthorized");
  }
}

export const authApi = {
  me: () => request<{ email?: string; user_id?: string }>("/auth/me"),
  sendCode: (email: string) =>
    request<Record<string, never>>("/auth/send-code", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  verify: (email: string, code: string) =>
    request<Record<string, never>>("/auth/verify", {
      method: "POST",
      body: JSON.stringify({ email, code }),
    }),
  logout: () => request<Record<string, never>>("/auth/logout", { method: "POST" }),
};

export const sessionApi = {
  list: () => request<Session[]>("/sessions"),
  create: () => request<Session>("/sessions", { method: "POST", body: JSON.stringify({ title: "New Chat" }) }),
  del: (id: string) => request<{ ok: boolean }>(`/sessions/${id}`, { method: "DELETE" }),
  messages: (id: string) => request<Message[]>(`/sessions/${id}/messages`),
};

export const approveApi = {
  submit: (requestId: string, approved: boolean) =>
    request<{ ok: boolean }>("/approve", {
      method: "POST",
      body: JSON.stringify({ request_id: requestId, approved }),
    }),
};
