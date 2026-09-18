# Controlled cohort replacement

Use a new cohort when an experiment definition must change after evidence collection
has begun. Preserve the original cohort and its observations so the change remains
auditable. This guide contains no private cohort manifest or operational snapshot.

## Procedure

1. Inspect readiness, health, and recorded observations for the affected cohort.
2. Record the reason for replacement and the decision about retaining or retiring the
   original experiment.
3. Review the new fixed strategy definitions, start session, execution methodology,
   benchmark, and simulated-capital assumptions.
4. Bootstrap the replacement with a new identity. Do not reuse or mutate an existing
   cohort's immutable definition.
5. Run preflight before the first official session and retain the resulting evidence.

Use the [CLI reference](cli-reference.md), [rollover runbook](cohort-rollover.md), and
[scheduler setup](cohort-scheduler-setup.md) for implemented commands and lifecycle rules.
The [challenger contract](../architecture/challenger-v1-contract.md) documents the
separate versioned experiment and its current integration boundary.
