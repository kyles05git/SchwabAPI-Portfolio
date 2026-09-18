import type { SafetyView, ApprovalsView, AuditView } from "@/lib/types";
import { SafetyPanel } from "../dashboard/SafetyPanel";
import { ApprovalsPanel } from "../dashboard/ApprovalsPanel";
import { AuditPanel } from "../dashboard/AuditPanel";

interface Props {
  safety: SafetyView;
  approvals: ApprovalsView;
  audit: AuditView;
  onKilled: () => void;
}

export function OperationsView({ safety, approvals, audit }: Props) {
  return (
    <section className="space-y-5" aria-label="Operations">
      <div className="grid gap-5 lg:grid-cols-2">
        <SafetyPanel view={safety} />
        <ApprovalsPanel view={approvals} />
      </div>
      <AuditPanel view={audit} />
    </section>
  );
}
