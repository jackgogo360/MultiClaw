import { API_BASE } from "./constants";
import { ensureCsrfToken, invalidateCsrfToken } from "./security";
import type { Message } from "./api";

export type AgentProfile = "reader" | "writer";
export interface AgentJob {
  job_id: string;
  status: string;
  version: number;
  request: { goal: string; profile: AgentProfile; model?: string; project_path: string };
  result?: { summary?: string; usage?: { total_tokens?: number } } | null;
  team_id?: string | null;
  member_id?: string | null;
  child_session_id?: string | null;
  child_run_id?: string | null;
}
export interface TeamMember {
  member_id: string;
  name: string;
  role: "leader" | "member";
  config: { profile?: AgentProfile; model?: string };
}
export interface TeamTask {
  task_id: string;
  title: string;
  objective: string;
  depends: string[];
  owner_member_id?: string | null;
  status: string;
  result?: string | { summary?: string } | null;
  version: number;
}
export interface TeamSummary {
  team_id: string;
  objective: string;
  status: string;
}
export interface AgentTeam extends TeamSummary {
  summary?: string;
  members: TeamMember[];
  tasks: TeamTask[];
}
export interface TeamMessage {
  message_id: string;
  sender_id: string | null;
  recipient_id: string | null;
  content: string;
}
export interface AgentTranscript {
  messages: Message[];
  progress?: Array<{ type: string; content?: string; name?: string }>;
  approvals: Array<{ approval_id: string; version: number; status: string; tool_name: string; tool_call_id: string; tool_input?: Record<string, unknown> }>;
}
export interface AgentChanges { patch: string; files: string[]; digest: string }
export interface CollaborationSnapshot { enabled: boolean; jobs: AgentJob[]; teams: TeamSummary[] }

async function request<T>(path: string, body?: object, retry = true): Promise<T> {
  const headers = new Headers();
  if (body) {
    headers.set("Content-Type", "application/json");
    headers.set("X-CSRF-Token", await ensureCsrfToken());
  }
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "include", headers, method: body ? "POST" : "GET",
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    const failure = await response.json().catch(() => null) as { detail?: unknown } | null;
    if (body && retry && response.status === 403 && failure?.detail === "CSRF validation failed") {
      await invalidateCsrfToken();
      return request<T>(path, body, false);
    }
    // Collaboration endpoints sanitize string details; validation arrays remain generic.
    const detail = typeof failure?.detail === "string" ? failure.detail.slice(0, 500) : null;
    throw new Error(response.status === 401 ? "Sign in to manage collaboration." : detail || `Collaboration request failed (${response.status}).`);
  }
  return response.status === 204 ? {} as T : response.json() as Promise<T>;
}
const scoped = (path: string, sessionId: string) => `${path}?session_id=${encodeURIComponent(sessionId)}`;
const agent = (id: string) => `/agents/${encodeURIComponent(id)}`;
const team = (id: string) => `/teams/${encodeURIComponent(id)}`;

export const collaborationApi = {
  list: (sessionId: string) => request<CollaborationSnapshot>(scoped("/collaboration", sessionId)),
  createAgent: (body: { session_id: string; goal: string; context?: string; profile: AgentProfile; project_path: string; model?: string }) => request<AgentJob>("/agents", body),
  transcript: (id: string, sessionId: string) => request<AgentTranscript>(scoped(`${agent(id)}/transcript`, sessionId)),
  cancelAgent: (id: string, sessionId: string) => request(`${agent(id)}/cancel`, { session_id: sessionId }),
  steerAgent: (id: string, sessionId: string, message: string) => request(`${agent(id)}/steer`, { session_id: sessionId, message }),
  changes: (id: string, sessionId: string) => request<AgentChanges>(scoped(`${agent(id)}/changes`, sessionId)),
  accept: (id: string, sessionId: string, digest: string) => request(`${agent(id)}/accept`, { session_id: sessionId, digest }),
  createTeam: (body: { session_id: string; objective: string; project_path: string; members: Array<{ name: string; role: "leader" | "member"; profile: AgentProfile; model?: string; instructions?: string }> }) => request<AgentTeam>("/teams", body),
  team: (id: string, sessionId: string) => request<AgentTeam>(scoped(team(id), sessionId)),
  createTask: (id: string, body: { session_id: string; title: string; objective: string; assigned_member_id?: string; depends_on?: string[] }) => request<TeamTask>(`${team(id)}/tasks`, body),
  messages: async (id: string, sessionId: string) => {
    const result = await request<TeamMessage[] | { messages: TeamMessage[] }>(scoped(`${team(id)}/messages`, sessionId));
    return Array.isArray(result) ? result : result.messages;
  },
  sendMessage: (id: string, sessionId: string, content: string, recipient_id?: string) => request(`${team(id)}/messages`, { session_id: sessionId, content, recipient_id }),
  cancelTeam: (id: string, sessionId: string) => request(`${team(id)}/cancel`, { session_id: sessionId }),
  teamChanges: (id: string, sessionId: string) => request<AgentChanges>(scoped(`${team(id)}/changes`, sessionId)),
  acceptTeam: (id: string, sessionId: string, digest: string) => request(`${team(id)}/accept`, { session_id: sessionId, digest }),
};
