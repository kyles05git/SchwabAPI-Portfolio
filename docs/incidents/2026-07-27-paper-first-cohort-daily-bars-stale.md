# Cohort freshness and immutable evidence

This engineering case study describes the failure mode and safeguards using generic
terms. Private incident timelines, cohort records, account information, and database
snapshots are not part of the public portfolio.

## Failure mode

An official paper cohort can receive incomplete end-of-session market data. If members
execute independently, some may mutate their simulated portfolios before other members
discover that required data is missing. The resulting comparison is incomplete.

## Safeguards

A cohort-wide preflight verifies the input requirements for every member before any
official paper execution begins. Missing data records an awaiting-data state within
the retry deadline. Permanent capability failures are distinguished from inputs that
may arrive later.

Once official observations exist, the cohort definition and recorded start session are
immutable. Corrections must preserve the original evidence. A replacement experiment
receives a separate identity and an explicit lifecycle decision.

## Implementation and verification

- [Cohort preflight](../../src/schwab_trader/cohort_preflight.py)
- [Freshness regression tests](../../tests/test_session_freshness.py)
- [Immutability regression tests](../../tests/test_incident_cohort_immutability.py)
- [Cohort rollover runbook](../operations/cohort-rollover.md)
- [Scheduler and retry behavior](../operations/cohort-scheduler-setup.md)
