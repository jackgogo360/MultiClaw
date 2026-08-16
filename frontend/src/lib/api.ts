import { API_BASE } from "./constants";
import { ensureCsrfToken, invalidateCsrfToken } from "./security";

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

export interface SecretMetadata {
  providerKind: string;
  providerName: string;
  secretName: string;
  maskedValue: string;
  updatedAt: number;
}

export interface AccountDeletionRequest {
  status: "scheduled";
  job_id: string;
  requested_at: number;
  purge_after: number;
}

export interface AccountDeletionStatus {
  status: "pending_purge";
  purge_after: number;
}

export interface ApprovalRecord {
  approval_id: string;
  status: string;
  version: number;
  expires_at: number;
  resolved_at: number | null;
}

type ApiOptions = RequestInit & {
  basePath?: string;
  csrf?: boolean;
  retryCsrf?: boolean;
};

type ErrorPayload = {
  detail?: unknown;
  code?: string;
  message?: string;
};

export class ApiError extends Error {
  status: number;
  code: string | null;
  retryAfter: number | null;

  constructor({
    status,
    code,
    message,
    retryAfter,
  }: {
    status: number;
    code?: string | null;
    message: string;
    retryAfter?: number | null;
  }) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code ?? null;
    this.retryAfter = retryAfter ?? null;
  }
}

export class AuthError extends ApiError {
  constructor(message = "Unauthorized") {
    super({ status: 401, message });
    this.name = "AuthError";
  }
}

function resolveUrl(path: string, basePath: string) {
  return `${basePath}${path}`;
}

function isMutationMethod(method: string) {
  return !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase());
}

function parseRetryAfter(value: string | null) {
  if (!value) {
    return null;
  }
  const seconds = Number.parseInt(value, 10);
  return Number.isFinite(seconds) ? seconds : null;
}

async function parseErrorResponse(response: Response) {
  const retryAfter = parseRetryAfter(response.headers.get("Retry-After"));
  const contentType = response.headers.get("content-type") ?? "";
  let payload: ErrorPayload | null = null;
  let fallbackText = "";

  if (contentType.includes("application/json")) {
    payload = (await response.json().catch(() => null)) as ErrorPayload | null;
  } else {
    fallbackText = (await response.text().catch(() => "")).trim();
  }

  const detail = payload?.detail;
  if (typeof detail === "string" && detail) {
    return new ApiError({
      status: response.status,
      code: typeof payload?.code === "string" ? payload.code : null,
      message: detail,
      retryAfter,
    });
  }

  if (detail && typeof detail === "object") {
    const code =
      typeof (detail as { code?: unknown }).code === "string"
        ? (detail as { code: string }).code
        : typeof payload?.code === "string"
          ? payload.code
          : null;
    const message =
      typeof (detail as { message?: unknown }).message === "string"
        ? (detail as { message: string }).message
        : typeof payload?.message === "string"
          ? payload.message
          : `HTTP ${response.status}`;
    return new ApiError({ status: response.status, code, message, retryAfter });
  }

  if (typeof payload?.message === "string" && payload.message) {
    return new ApiError({
      status: response.status,
      code: typeof payload?.code === "string" ? payload.code : null,
      message: payload.message,
      retryAfter,
    });
  }

  return new ApiError({
    status: response.status,
    code: null,
    message: fallbackText || `HTTP ${response.status}`,
    retryAfter,
  });
}

function isCsrfFailure(error: ApiError) {
  return error.status === 403 && error.message === "CSRF validation failed";
}

async function request<T>(path: string, options: ApiOptions = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const csrf = options.csrf ?? isMutationMethod(method);
  const headers = new Headers(options.headers);

  if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  if (csrf) {
    headers.set("X-CSRF-Token", await ensureCsrfToken());
  }

  const response = await fetch(resolveUrl(path, options.basePath ?? API_BASE), {
    credentials: "include",
    ...options,
    headers,
  });

  if (response.status === 401) {
    const error = await parseErrorResponse(response);
    throw new AuthError(error.message);
  }

  if (!response.ok) {
    const error = await parseErrorResponse(response);
    if (csrf && options.retryCsrf !== false && isCsrfFailure(error)) {
      await invalidateCsrfToken();
      return request<T>(path, { ...options, retryCsrf: false });
    }
    throw error;
  }

  if (response.status === 204) {
    return {} as T;
  }

  return response.json() as Promise<T>;
}

function formatSecretProvider(providerKind: string, providerName: string) {
  return encodeURIComponent(`${providerKind}:${providerName}`);
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
  sendDeletionRecoveryCode: (email: string) =>
    request<Record<string, never>>("/auth/deletion-recovery/send-code", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  verifyDeletionRecoveryCode: (email: string, code: string) =>
    request<Record<string, never>>("/auth/deletion-recovery/verify", {
      method: "POST",
      body: JSON.stringify({ email, code }),
    }),
};

export const sessionApi = {
  list: () => request<Session[]>("/sessions"),
  create: () =>
    request<Session>("/sessions", {
      method: "POST",
      body: JSON.stringify({ title: "New Chat" }),
    }),
  del: (id: string) => request<{ ok: boolean }>(`/sessions/${id}`, { method: "DELETE" }),
  messages: (id: string) => request<Message[]>(`/sessions/${id}/messages`),
};

export const secretApi = {
  list: () => request<SecretMetadata[]>("/secrets"),
  put: (providerKind: string, providerName: string, secretName: string, value: string) =>
    request<SecretMetadata>(`/secrets/${formatSecretProvider(providerKind, providerName)}/${encodeURIComponent(secretName)}`, {
      method: "PUT",
      body: JSON.stringify({ value }),
    }),
  del: (providerKind: string, providerName: string, secretName: string) =>
    request<{ ok: boolean }>(`/secrets/${formatSecretProvider(providerKind, providerName)}/${encodeURIComponent(secretName)}`, {
      method: "DELETE",
    }),
  test: (providerKind: string, providerName: string, secretName: string) =>
    request<{ ok: boolean }>(`/secrets/${formatSecretProvider(providerKind, providerName)}/${encodeURIComponent(secretName)}/test`, {
      method: "POST",
    }),
};

export const accountApi = {
  requestDeletion: () => request<AccountDeletionRequest>("/account/deletion", { method: "POST" }),
  status: () => request<AccountDeletionStatus>("/account/deletion", { method: "GET", csrf: false }),
  recover: () => request<{ ok: boolean }>("/account/deletion/recover", { method: "POST" }),
};

export const approveApi = {
  getApproval: (approvalId: string) =>
    request<ApprovalRecord>(`/approvals/${approvalId}`, { method: "GET", csrf: false }),
  submit: (approvalId: string, approved: boolean, version: number) =>
    request<ApprovalRecord>(`/approvals/${approvalId}/decision`, {
      method: "POST",
      body: JSON.stringify({ approved, version }),
    }),
};
