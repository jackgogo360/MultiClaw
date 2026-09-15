import { useEffect, useState, useSyncExternalStore } from "react";
import type { PlanReference } from "@/lib/api";
import { planStore } from "@/lib/plan-store";
import { PlanDecisionControls } from "./PlanDecisionControls";
import { PlanStepList } from "./PlanStepList";

export function PlanCard({ reference }: { reference: PlanReference }) {
  const state = useSyncExternalStore(planStore.subscribe, planStore.getSnapshot);
  const [selection, setSelection] = useState<{
    version: number;
    aggregateVersion: number;
  } | null>(null);
  const plan = state.plans[reference.plan_id];
  const error = state.errors[reference.plan_id];

  useEffect(() => {
    if (state.sessionId === reference.session_id && !plan) {
      void planStore.refresh(reference);
    }
  }, [plan, reference, state.sessionId]);

  if (state.sessionId !== reference.session_id) return null;
  if (!plan && error) {
    return (
      <section className="plan-card" role="alert">
        <p>{error}</p>
        <button onClick={() => void planStore.refresh(reference)}>Retry loading Plan</button>
      </section>
    );
  }
  if (!plan) return <section className="plan-card" aria-busy="true">Loading Plan…</section>;

  const selected =
    selection?.aggregateVersion === plan.aggregate_version
      ? selection.version
      : plan.current_version;
  const version =
    plan.versions.find((item) => item.plan_version === selected) ??
    plan.versions.find((item) => item.plan_version === plan.current_version)!;
  const succeeded = version.steps.filter((step) => step.status === "succeeded").length;
  const waiting =
    plan.status === "awaiting_approval" &&
    plan.run_status === "awaiting_user" &&
    plan.active_run_id !== null &&
    selected === plan.current_version;

  return (
    <section className="plan-card" aria-label="Execution plan">
      <header>
        <div>
          <span>Plan</span>
          <span className="plan-status">{plan.status.replace(/_/g, " ")}</span>
        </div>
        <label>
          Version
          <select
            value={version.plan_version}
            onChange={(event) =>
              setSelection({
                version: Number(event.target.value),
                aggregateVersion: plan.aggregate_version,
              })
            }
          >
            {plan.versions.map((item) => (
              <option key={item.plan_version} value={item.plan_version}>v{item.plan_version}</option>
            ))}
          </select>
        </label>
      </header>
      <h3>{version.objective}</h3>
      {plan.run_status && <p>Latest run: {plan.run_status.replace(/_/g, " ")}</p>}
      <progress value={succeeded} max={version.steps.length || 1}>
        {succeeded}/{version.steps.length}
      </progress>
      <p>{succeeded} of {version.steps.length} steps succeeded</p>
      {version.constraints.length > 0 && (
        <ul>{version.constraints.map((item) => <li key={item}>{item}</li>)}</ul>
      )}
      {error && <p role="alert">{error}</p>}
      <PlanStepList version={version} />
      <PlanDecisionControls
        plan={plan}
        reference={reference}
        allowDecision={waiting}
        allowRunControls={selected === plan.current_version}
      />
    </section>
  );
}
