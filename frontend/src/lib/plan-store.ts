import { planApi, type Message, type PlanDataPart, type PlanReference, type PlanView } from "./api";

type Listener = () => void;
type PlanStoreSnapshot = Readonly<{ sessionId: string | null; plans: Readonly<Record<string, PlanView>>; loading: Readonly<Record<string, boolean>>; errors: Readonly<Record<string, string>> }>;
let snapshot: PlanStoreSnapshot = { sessionId: null, plans: {}, loading: {}, errors: {} };
const listeners = new Set<Listener>();
const refreshEpochs = new Map<string, number>();
let nextRefreshEpoch = 0;
const publish = (next: PlanStoreSnapshot) => { snapshot = next; listeners.forEach((listener) => listener()); };

function readPlanReference(value: unknown): PlanReference | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  if (item.schema_version !== 1 || Object.keys(item).some((key) => key === "schema_version" ? false : item[key] == null)) return null;
  if (!["tenant_id", "workspace_id", "session_id", "run_id", "plan_id"].every((key) => typeof item[key] === "string")) return null;
  if (!["plan_version", "aggregate_version"].every((key) => typeof item[key] === "number" && Number.isInteger(item[key]) && (item[key] as number) >= 1)) return null;
  return item as unknown as PlanReference;
}

async function refresh(reference: PlanReference): Promise<void> {
  if (snapshot.sessionId !== reference.session_id) return;
  const key = `${reference.session_id}:${reference.plan_id}`;
  const epoch = ++nextRefreshEpoch; refreshEpochs.set(key, epoch);
  publish({ ...snapshot, loading: { ...snapshot.loading, [reference.plan_id]: true }, errors: { ...snapshot.errors, [reference.plan_id]: "" } });
  try {
    const plan = await planApi.get(reference.session_id, reference.plan_id);
    if (snapshot.sessionId !== reference.session_id || refreshEpochs.get(key) !== epoch) return;
    const current = snapshot.plans[reference.plan_id];
    if (current && current.aggregate_version > plan.aggregate_version) {
      publish({ ...snapshot, loading: { ...snapshot.loading, [reference.plan_id]: false } });
      return;
    }
    publish({ ...snapshot, plans: { ...snapshot.plans, [plan.plan_id]: plan }, loading: { ...snapshot.loading, [plan.plan_id]: false }, errors: { ...snapshot.errors, [plan.plan_id]: "" } });
  } catch (error) {
    if (snapshot.sessionId !== reference.session_id || refreshEpochs.get(key) !== epoch) return;
    publish({ ...snapshot, loading: { ...snapshot.loading, [reference.plan_id]: false }, errors: { ...snapshot.errors, [reference.plan_id]: error instanceof Error ? error.message : "Plan refresh failed" } });
  }
}

export const planStore = {
  subscribe(listener: Listener) { listeners.add(listener); return () => listeners.delete(listener); },
  getSnapshot() { return snapshot; },
  reset(sessionId: string | null) { refreshEpochs.clear(); publish({ sessionId, plans: {}, loading: {}, errors: {} }); },
  async hydrateSession(sessionId: string, messages: Message[]) { this.reset(sessionId); const refs = messages.flatMap((message) => message.parts ?? []).map((part) => part.data).map(readPlanReference).filter((ref): ref is PlanReference => ref !== null); await Promise.all(refs.map(refresh)); },
  acceptDataPart(part: PlanDataPart) { const ref = readPlanReference(part.data); if (!ref || ref.session_id !== snapshot.sessionId) return; const current = snapshot.plans[ref.plan_id]; if (!current || ref.aggregate_version >= current.aggregate_version) void refresh(ref); },
  refresh,
};
