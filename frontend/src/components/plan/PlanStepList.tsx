import type { PlanVersionView } from "@/lib/api";
export function PlanStepList({ version }: { version: PlanVersionView }) {
  return <ol className="plan-step-list" aria-label={`Plan version ${version.plan_version} steps`}>
    {version.steps.map((step) => { const active = step.status === "running" || step.status === "failed_retryable"; return <li key={step.step_id} className="plan-step" data-status={step.status}><details open={active}><summary><span className="plan-step-index">{step.ordinal}</span><span>{step.title}</span><span className="plan-status" aria-label={`Status: ${step.status}`}>{step.status.replace(/_/g, " ")}</span></summary><p>{step.description}</p><p><strong>Expected:</strong> {step.expected_outcome}</p>{step.depends_on.length > 0 && <p><strong>Depends on:</strong> {step.depends_on.join(", ")}</p>}{step.result_summary && <p><strong>Result:</strong> {step.result_summary}</p>}{step.error_detail && <p role="alert"><strong>Error:</strong> {step.error_detail}</p>}</details></li>; })}
  </ol>;
}
