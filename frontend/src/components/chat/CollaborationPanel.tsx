import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApprovalToolUI } from "@/components/approval/ApprovalToolUI";
import { useAuth } from "@/lib/auth-context-store";
import { sessionStore } from "@/lib/session-store";
import { collaborationApi as api, type AgentChanges, type AgentJob, type AgentProfile, type AgentTeam, type AgentTranscript, type CollaborationSnapshot, type TeamMessage } from "@/lib/collaboration-api";

const inputClass = "w-full rounded border border-border bg-input p-2 text-xs";
const buttonClass = "rounded border border-border px-3 py-1 text-xs hover:border-accent disabled:opacity-40";
const terminal = new Set(["completed", "cancelled", "failed", "failed_terminal", "accepted", "blocked", "blocked_corrupt", "blocked_incompatible"]);

function usePolling<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loader = useRef(load);
  useEffect(() => { loader.current = load; });
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const next = await loader.current();
        if (!disposed) { setData(next); setError(null); }
      } catch (failure) {
        if (!disposed) setError(failure instanceof Error ? failure.message : "Could not refresh collaboration.");
      } finally {
        if (!disposed) timer = setTimeout(refresh, 2000);
      }
    }
    void refresh();
    return () => { disposed = true; clearTimeout(timer); };
  }, []);
  return { data, error };
}

function useAction(sessionId: string) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function act(action: () => Promise<unknown>, success: string, done?: () => void) {
    if (busy) return;
    setBusy(true); setError(null); setNotice(null);
    const inScope = () => mounted.current && sessionStore.getSnapshot().currentId === sessionId;
    try {
      await action();
      if (inScope()) { setNotice(success); done?.(); }
    } catch (failure) {
      if (inScope()) setError(failure instanceof Error ? failure.message : "Collaboration action failed.");
    } finally { if (inScope()) setBusy(false); }
  }
  return { busy, notice, error, act };
}

function Feedback({ error, notice }: { error?: string | null; notice?: string | null }) {
  return <>{notice && <p role="status" className="text-xs text-muted-foreground">{notice}</p>}{error && <p role="alert" className="text-xs text-danger">{error}</p>}</>;
}

function ProfileField({ label, value, onChange }: { label: string; value: AgentProfile; onChange: (profile: AgentProfile) => void }) {
  return <label className="block text-xs">{label}<select className={inputClass} value={value} onChange={(event) => onChange(event.target.value as AgentProfile)}><option value="reader">Reader · inspect only</option><option value="writer">Writer · isolated changes</option></select></label>;
}

function JobDetail({ job, sessionId }: { job: AgentJob; sessionId: string }) {
  const { data, error } = usePolling<AgentTranscript>(() => api.transcript(job.job_id, sessionId));
  const action = useAction(sessionId);
  const [instruction, setInstruction] = useState("");
  const [changes, setChanges] = useState<AgentChanges | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const running = !terminal.has(job.status);
  const progress = (data?.progress ?? []).slice(-200).map((event) => {
    if (event.type === "token") return event.content ?? "";
    if (event.type === "tool_call" && event.name) return `\nUsing ${event.name}…\n`;
    return "";
  }).join("").slice(-24000);
  return <section className="mt-3 space-y-2 rounded border border-border p-3" aria-label="Selected agent">
    <p className="text-xs">{job.request.goal} · {job.request.profile} · {job.status}</p>
    {job.result?.summary && <p className="whitespace-pre-wrap text-xs">{job.result.summary}</p>}
    {job.result?.usage?.total_tokens != null && <p className="text-xs text-muted-foreground">{job.result.usage.total_tokens.toLocaleString()} tokens</p>}
    {running && progress && <pre aria-label="Agent live progress" className="max-h-48 overflow-auto whitespace-pre-wrap rounded bg-background p-2 text-xs">{progress}</pre>}
    <details><summary className="cursor-pointer text-xs">Transcript</summary><div className="mt-2 max-h-52 space-y-2 overflow-auto">
      {data?.messages.map((message) => <div key={message.id} className="text-xs"><span className="text-muted-foreground">{message.role}</span><p className="whitespace-pre-wrap">{message.content}</p></div>)}
      {data?.messages.length === 0 && <p className="text-xs text-muted-foreground">No messages yet.</p>}
    </div></details>
    {data?.approvals.filter((approval) => approval.status === "pending" || approval.status === "awaiting_user").map((approval) => <ApprovalToolUI key={approval.approval_id} approvalId={approval.approval_id} toolCallId={approval.tool_call_id} toolName={approval.tool_name} args={approval.tool_input ?? {}} status={{ type: "requires-action" }} />)}
    {running && <>
      <textarea aria-label="Agent instruction" placeholder="Instruction for the next model call" value={instruction} onChange={(event) => setInstruction(event.target.value)} disabled={action.busy} maxLength={8000} rows={2} className={inputClass} />
      <div className="flex gap-2"><button className={buttonClass} disabled={action.busy || !instruction.trim()} onClick={() => void action.act(() => api.steerAgent(job.job_id, sessionId, instruction.trim()), "Instruction added.", () => setInstruction(""))}>Add instruction</button><button className={buttonClass} disabled={action.busy} onClick={() => void action.act(() => api.cancelAgent(job.job_id, sessionId), "Cancellation requested.")}>Cancel agent</button></div>
    </>}
    {job.request.profile === "writer" && <>
      <button className={buttonClass} disabled={action.busy} onClick={() => void action.act(async () => {
        const next = await api.changes(job.job_id, sessionId);
        if (sessionStore.getSnapshot().currentId === sessionId) { setChanges(next); setReviewed(false); }
      }, "Changes ready for review.")}>Review changes</button>
      {changes && <div className="space-y-2">
        <p className="text-xs">{changes.files.length} changed files</p>
        <pre aria-label="Agent changes patch" className="max-h-64 overflow-auto rounded bg-background p-2 text-xs">{changes.patch || "No changes."}</pre>
        {changes.patch && (job.team_id
          ? <p className="text-xs text-muted-foreground">Accept member changes from the completed team's review.</p>
          : <><label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} />I reviewed this patch</label><button className={buttonClass} disabled={action.busy || !reviewed || running} onClick={() => void action.act(() => api.accept(job.job_id, sessionId, changes.digest), "Reviewed changes accepted.", () => { setChanges(null); setReviewed(false); })}>Accept reviewed changes</button><p className="text-xs text-muted-foreground">Acceptance applies this exact patch to the project. Refresh review if changes have moved.</p></>)}
      </div>}
    </>}
    <Feedback error={action.error ?? error} notice={action.notice} />
  </section>;
}

function TeamDetail({ teamId, sessionId }: { teamId: string; sessionId: string }) {
  const { data, error } = usePolling<{ team: AgentTeam; messages: TeamMessage[] }>(async () => {
    const [team, messages] = await Promise.all([api.team(teamId, sessionId), api.messages(teamId, sessionId)]);
    return { team, messages };
  });
  const action = useAction(sessionId);
  const [title, setTitle] = useState("");
  const [objective, setObjective] = useState("");
  const [owner, setOwner] = useState("");
  const [depends, setDepends] = useState<string[]>([]);
  const [message, setMessage] = useState("");
  const [recipient, setRecipient] = useState("");
  const [changes, setChanges] = useState<AgentChanges | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const team = data?.team;
  const memberName = (id: string | null | undefined) => team?.members.find((member) => member.member_id === id)?.name ?? (id ? "Agent" : "You");
  if (!team) return <Feedback error={error} notice="Loading team…" />;
  const running = !terminal.has(team.status);
  return <section className="mt-3 space-y-3 rounded border border-border p-3" aria-label="Selected team">
    <p className="text-xs">{team.objective} · {team.status}</p>
    {team.summary && <div aria-label="Team final report" className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-background p-2 text-xs">{team.summary}</div>}
    {team.status === "completed" && <div className="space-y-2">
      <button className={buttonClass} disabled={action.busy} onClick={() => {
        setChanges(null); setReviewed(false);
        void action.act(async () => {
          const next = await api.teamChanges(teamId, sessionId);
          if (sessionStore.getSnapshot().currentId === sessionId) setChanges(next);
        }, "Team changes ready for review.");
      }}>Review team changes</button>
      {changes && <>
        <p className="text-xs">{changes.files.length} changed files</p>
        <pre aria-label="Team changes patch" className="max-h-64 overflow-auto rounded bg-background p-2 text-xs">{changes.patch || "No changes."}</pre>
        {changes.patch && <>
          <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={reviewed} disabled={action.busy} onChange={(event) => setReviewed(event.target.checked)} />I reviewed this team patch</label>
          <button className={buttonClass} disabled={action.busy || !reviewed} onClick={() => void action.act(() => api.acceptTeam(teamId, sessionId, changes.digest), "Reviewed team changes accepted.", () => { setChanges(null); setReviewed(false); })}>Accept reviewed team changes</button>
          <p className="text-xs text-muted-foreground">Acceptance applies the displayed writer patches together. Overlapping files must be resolved before acceptance.</p>
        </>}
      </>}
    </div>}
    <div className="flex flex-wrap gap-2" aria-label="Team members">{team.members.map((member) => <span key={member.member_id} className="rounded bg-background p-2 text-xs">{member.name} · {member.role} · {member.config?.profile ?? "reader"}</span>)}</div>
    <div aria-label="Team task board" className="space-y-2">{team.tasks.map((task) => <div key={task.task_id} className="rounded border border-border p-2 text-xs">
      <p>{task.title} · {task.status} · {task.owner_member_id ? memberName(task.owner_member_id) : "Unassigned"}</p><p className="text-muted-foreground">{task.objective}</p>
      {task.depends?.length > 0 && <p>Depends on: {task.depends.map((id) => team.tasks.find((entry) => entry.task_id === id)?.title ?? "Task").join(", ")}</p>}
      {task.result && <p className="whitespace-pre-wrap">{typeof task.result === "string" ? task.result : task.result.summary}</p>}
    </div>)}{team.tasks.length === 0 && <p className="text-xs text-muted-foreground">No tasks yet. Add a task to start a member.</p>}</div>
    {running && <form className="space-y-2" onSubmit={(event) => { event.preventDefault(); void action.act(() => api.createTask(teamId, { session_id: sessionId, title: title.trim(), objective: objective.trim(), ...(owner ? { assigned_member_id: owner } : {}), depends_on: depends }), "Team task created.", () => { setTitle(""); setObjective(""); setDepends([]); }); }}>
      <fieldset disabled={action.busy} className="space-y-2">
      <input aria-label="Team task title" placeholder="Task title" value={title} onChange={(event) => setTitle(event.target.value)} required maxLength={160} className={inputClass} />
      <textarea aria-label="Team task objective" placeholder="Task objective" value={objective} onChange={(event) => setObjective(event.target.value)} required maxLength={10000} rows={2} className={inputClass} />
      <label className="block text-xs">Assign to<select className={inputClass} value={owner} onChange={(event) => setOwner(event.target.value)}><option value="">Unassigned</option>{team.members.map((member) => <option key={member.member_id} value={member.member_id}>{member.name}</option>)}</select></label>
      {team.tasks.length > 0 && <fieldset className="space-y-1"><legend className="text-xs">Dependencies ({depends.length}/20)</legend>{team.tasks.map((task) => <label key={task.task_id} className="flex items-center gap-2 text-xs"><input type="checkbox" checked={depends.includes(task.task_id)} disabled={depends.length >= 20 && !depends.includes(task.task_id)} onChange={(event) => setDepends((previous) => event.target.checked ? previous.length < 20 ? [...previous, task.task_id] : previous : previous.filter((id) => id !== task.task_id))} />{task.title}</label>)}</fieldset>}
      <button className={buttonClass} disabled={action.busy || !title.trim() || !objective.trim()}>Create task</button>
      </fieldset>
    </form>}
    <details><summary className="cursor-pointer text-xs">Team messages</summary><div className="my-2 max-h-40 space-y-2 overflow-auto">{data.messages.map((entry) => <div key={entry.message_id} className="text-xs"><p className="text-muted-foreground">{memberName(entry.sender_id)} → {entry.recipient_id ? memberName(entry.recipient_id) : "Everyone"}</p><p className="whitespace-pre-wrap">{entry.content}</p></div>)}{data.messages.length === 0 && <p className="text-xs text-muted-foreground">No messages yet.</p>}</div></details>
    {running && <form className="space-y-2" onSubmit={(event) => { event.preventDefault(); void action.act(() => api.sendMessage(teamId, sessionId, message.trim(), recipient || undefined), "Message sent.", () => setMessage("")); }}>
      <fieldset disabled={action.busy} className="space-y-2">
      <label className="block text-xs">Message recipient<select className={inputClass} value={recipient} onChange={(event) => setRecipient(event.target.value)}><option value="">Everyone</option>{team.members.map((member) => <option key={member.member_id} value={member.member_id}>{member.name}</option>)}</select></label>
      <textarea aria-label="Team message" placeholder="Message the team" value={message} onChange={(event) => setMessage(event.target.value)} required maxLength={8000} rows={2} className={inputClass} />
      <div className="flex gap-2"><button className={buttonClass} disabled={action.busy || !message.trim()}>Send message</button><button type="button" className={buttonClass} disabled={action.busy} onClick={() => void action.act(() => api.cancelTeam(teamId, sessionId), "Team cancellation requested.")}>Cancel team</button></div>
      </fieldset>
    </form>}
    <Feedback error={action.error ?? error} notice={action.notice} />
  </section>;
}

function ScopedPanel({ sessionId }: { sessionId: string }) {
  const { data, error } = usePolling<CollaborationSnapshot>(() => api.list(sessionId));
  const action = useAction(sessionId);
  const [selection, setSelection] = useState<{ type: "agent" | "team"; id: string } | null>(null);
  const [kind, setKind] = useState<"agent" | "team">("agent");
  const [goal, setGoal] = useState("");
  const [project, setProject] = useState(".");
  const [profile, setProfile] = useState<AgentProfile>("reader");
  const [model, setModel] = useState("");
  const [peers, setPeers] = useState<Array<{ id: number; name: string; profile: AgentProfile; model: string; instructions: string }>>([
    { id: 1, name: "Member1", profile: "reader", model: "", instructions: "" },
  ]);
  const nextPeerId = useRef(2);
  const [context, setContext] = useState("");
  const validNames = peers.every((peer) => /^[A-Za-z0-9_-]{1,80}$/.test(peer.name))
    && new Set(["Leader", ...peers.map((peer) => peer.name)]).size === peers.length + 1;
  const updatePeer = (id: number, patch: Partial<(typeof peers)[number]>) => setPeers((previous) => previous.map((peer) => peer.id === id ? { ...peer, ...patch } : peer));
  const selectedJob = selection?.type === "agent" ? data?.jobs.find((job) => job.job_id === selection.id) : null;
  return <details className="border-b border-border bg-surface px-4 py-2">
    <summary className="cursor-pointer text-sm text-muted-foreground">Agents & teams{data?.enabled ? ` · ${data.jobs.length} agents · ${data.teams.length} teams` : ""}</summary>
    <div className="mt-2 max-h-[32rem] space-y-3 overflow-auto">
      <Feedback error={error} />
      {data?.enabled === false && <p className="text-xs text-muted-foreground">Collaboration is disabled. Set collaboration.enabled = true in the server configuration to create durable agents and teams.</p>}
      {data?.enabled && <>
        <p className="text-xs text-muted-foreground">Agents continue while this page is closed. Writers prepare isolated changes for your review.</p>
        <form className="space-y-2" onSubmit={(event) => {
          event.preventDefault();
          if (kind === "team" && !validNames) return;
          void action.act(async () => {
            if (kind === "agent") {
              const job = await api.createAgent({ session_id: sessionId, goal: goal.trim(), context: context.trim() || undefined, profile, project_path: project.trim() || ".", model: model.trim() || undefined });
              if (sessionStore.getSnapshot().currentId === sessionId) setSelection({ type: "agent", id: job.job_id });
            } else {
              const team = await api.createTeam({ session_id: sessionId, objective: goal.trim(), project_path: project.trim() || ".", members: [
                { name: "Leader", role: "leader", profile, model: model.trim() || undefined, instructions: context.trim() || undefined },
                ...peers.map((peer) => ({ name: peer.name, role: "member" as const, profile: peer.profile, model: peer.model.trim() || undefined, instructions: peer.instructions.trim() || undefined })),
              ] });
              if (sessionStore.getSnapshot().currentId === sessionId) setSelection({ type: "team", id: team.team_id });
            }
          }, kind === "agent" ? "Agent created." : "Team created.", () => { setGoal(""); setContext(""); });
        }}>
          <fieldset disabled={action.busy} className="space-y-2">
          <label className="block text-xs">Create<select className={inputClass} value={kind} onChange={(event) => setKind(event.target.value as "agent" | "team")}><option value="agent">Independent agent</option><option value="team">Agent team</option></select></label>
          <textarea aria-label={kind === "agent" ? "Agent goal" : "Team objective"} placeholder={kind === "agent" ? "What should the agent do?" : "What should the team achieve?"} value={goal} onChange={(event) => setGoal(event.target.value)} required maxLength={10000} rows={2} className={inputClass} />
          <label className="block text-xs">Project directory (relative to your workspace)<input className={inputClass} value={project} onChange={(event) => setProject(event.target.value)} required maxLength={1000} pattern="[^/].*" title="Use a directory relative to your workspace." /></label>
          <div className="grid gap-2 sm:grid-cols-2"><ProfileField label={kind === "agent" ? "Agent access" : "Leader access"} value={profile} onChange={setProfile} /><label className="block text-xs">{kind === "agent" ? "Model (optional)" : "Leader model (optional)"}<input className={inputClass} value={model} onChange={(event) => setModel(event.target.value)} maxLength={255} placeholder="Server default" /></label></div>
          <textarea aria-label={kind === "agent" ? "Agent context" : "Leader instructions"} placeholder={kind === "agent" ? "Optional context" : "Optional leader instructions"} value={context} onChange={(event) => setContext(event.target.value)} maxLength={kind === "agent" ? 10000 : 8000} rows={2} className={inputClass} />
          {kind === "team" && <div className="space-y-2">
            <p className="text-xs text-muted-foreground">Leader + {peers.length} {peers.length === 1 ? "member" : "members"} · up to 6 agents</p>
            {peers.map((peer, index) => <fieldset key={peer.id} className="space-y-2 rounded border border-border p-2">
              <legend className="text-xs">Member {index + 1}</legend>
              <label className="block text-xs">Name<input className={inputClass} value={peer.name} onChange={(event) => updatePeer(peer.id, { name: event.target.value })} required maxLength={80} pattern={"[A-Za-z0-9_\\-]+"} title="Use a unique name with letters, numbers, underscores, or hyphens. Leader is reserved." /></label>
              <div className="grid gap-2 sm:grid-cols-2"><ProfileField label="Access" value={peer.profile} onChange={(value) => updatePeer(peer.id, { profile: value })} /><label className="block text-xs">Model (optional)<input className={inputClass} value={peer.model} onChange={(event) => updatePeer(peer.id, { model: event.target.value })} maxLength={255} placeholder="Server default" /></label></div>
              <textarea aria-label={`Member ${index + 1} instructions`} placeholder="Optional member instructions" className={inputClass} value={peer.instructions} onChange={(event) => updatePeer(peer.id, { instructions: event.target.value })} maxLength={8000} rows={2} />
              <button type="button" className={buttonClass} disabled={peers.length === 1} onClick={() => setPeers((previous) => previous.filter((entry) => entry.id !== peer.id))}>Remove member {index + 1}</button>
            </fieldset>)}
            <button type="button" className={buttonClass} disabled={peers.length >= 5} onClick={() => {
              const id = nextPeerId.current++;
              setPeers((previous) => {
                if (previous.length >= 5) return previous;
                let suffix = id;
                while (previous.some((peer) => peer.name === `Member${suffix}`)) suffix++;
                return [...previous, { id, name: `Member${suffix}`, profile: "reader", model: "", instructions: "" }];
              });
            }}>Add member</button>
            {!validNames && <p role="alert" className="text-xs text-danger">Member names must be unique, use letters, numbers, underscores or hyphens, and differ from Leader.</p>}
          </div>}
          <button className={buttonClass} disabled={action.busy || !goal.trim() || (kind === "team" && !validNames)}>Create {kind}</button>
          </fieldset>
        </form>
        <Feedback error={action.error} notice={action.notice} />
        <div className="flex flex-wrap gap-2" aria-label="Collaboration selection">
          {data.jobs.map((job) => <button key={job.job_id} className={buttonClass} aria-pressed={selection?.type === "agent" && selection.id === job.job_id} onClick={() => setSelection({ type: "agent", id: job.job_id })}>{job.request.goal.slice(0, 60)} · {job.status}</button>)}
          {data.teams.map((team) => <button key={team.team_id} className={buttonClass} aria-pressed={selection?.type === "team" && selection.id === team.team_id} onClick={() => setSelection({ type: "team", id: team.team_id })}>Team: {team.objective.slice(0, 60)} · {team.status}</button>)}
        </div>
        {selectedJob && <JobDetail key={selectedJob.job_id} job={selectedJob} sessionId={sessionId} />}
        {selection?.type === "team" && <TeamDetail key={selection.id} teamId={selection.id} sessionId={sessionId} />}
      </>}
    </div>
  </details>;
}

export function CollaborationPanel() {
  const { userId, accountStatus } = useAuth();
  const { currentId, resetVersion } = useSyncExternalStore(sessionStore.subscribe, sessionStore.getSnapshot);
  if (!currentId || !userId || accountStatus !== "active") return null;
  return <ScopedPanel key={`${userId}:${currentId}:${resetVersion}`} sessionId={currentId} />;
}
