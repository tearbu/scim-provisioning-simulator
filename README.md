# SCIM Provisioning Simulator

This simulates SCIM 2.0 user lifecycle provisioning between an HR source-of-truth roster and multiple target applications. It runs a full joiner / mover / leaver sync cycle, models the failure modes that show up in real identity onboarding projects, and produces the findings a governance review actually cares about: **orphaned active accounts for leavers, attribute drift, and duplicate accounts**. It also does a per-application reconciliation of HR identities against application accounts.

## The problem it solves

In production IdP deployments (Okta, Entra ID, SailPoint, etc.), provisioning failures are quiet. A deprovisioning call times out. A required attribute is missing in the HR feed. A schema mismatch blocks an update. The sync job "completes", but a terminated administrator still has an active account, or a transferred employee's entitlements still reflect their old department. This tool reproduces that failure surface in a safe, offline sandbox so identity engineers and auditors can study it, demonstrate it, and test detection of provisioning defects before they become access-review findings.

## What it demonstrates

- Joiner-Mover-Leaver lifecycle automation driven by an HR source of truth
- Connector-style failure handling: API errors (500/503), schema validation errors (400), and missing required attributes
- Consequence analysis: failed deprovisioning of a privileged leaver is a **Critical** orphan; failed creates and unresolved attribute drift are reported with root cause
- Reconciliation that compares **identity sets**, not just counts. A failed create plus a failed deprovision cancel out numerically while the account population is still wrong (see Payroll in the demo output)

## How to run

Demo mode (bundled fictional sample data: 8-person HR roster, 3 target apps, with failure cases built in):

```bash
python3 simulator.py --demo
```

Real usage: supply your own files in the same JSON formats as `sample_data/hr_roster.json` and `sample_data/apps.json`:

```bash
python3 simulator.py --hr path/to/roster.json --apps path/to/apps.json
```

Requirements: Python 3.8+, standard library only. No network access is performed. Target application "API calls" are simulated and failures are driven by the `simulated_failures` entries in the apps file.

## Demo output (actual run)

```
========================================================================
SCIM PROVISIONING SIMULATION — SYNC RUN REPORT
========================================================================
HR roster: 8 identities (5 active, 3 terminated)
Target applications: 3

--- Email Suite -------------------------------------------------------
  CREATE   E1002 -> Email Suite: 201 Created
  CREATE   E1007 -> Email Suite: 201 Created
  DEPROV  E1004 -> Email Suite: FAILED (API 500: directory connector timeout)
  DEPROV  E1005 -> Email Suite: 200 OK (active=false)
  DEPROV  E1008 -> Email Suite: 200 OK (active=false)
  Result: 6 active account(s) after sync

--- CRM ---------------------------------------------------------------
  CREATE   E1002 -> CRM: FAILED (Missing required attribute(s): cost_center)
  UPDATE   E1003 -> CRM: FAILED (API 400: attribute 'costCenter' rejected by target schema)
  CREATE   E1007 -> CRM: 201 Created
  DEPROV  E1005 -> CRM: 200 OK (active=false)
  Result: 4 active account(s) after sync

--- Payroll -----------------------------------------------------------
  CREATE   E1002 -> Payroll: FAILED (Missing required attribute(s): cost_center)
  CREATE   E1007 -> Payroll: 201 Created
  DEPROV  E1008 -> Payroll: FAILED (API 503: upstream service unavailable)
  Result: 5 active account(s) after sync

========================================================================
FINDINGS
========================================================================
[Critical] Orphaned active account — Email Suite / E1004
           Leaver's account is still ACTIVE because deprovisioning failed: API 500: directory connector timeout Account has privileged access.
[High    ] Orphaned active account — Payroll / E1008
           Leaver's account is still ACTIVE because deprovisioning failed: API 503: upstream service unavailable
[Medium  ] Duplicate account — CRM / E1002
           Two active accounts for one employee: daniel.reyes@example.com and d.reyes@example.com
[Medium  ] Provisioning failure (create) — CRM / E1002
           Joiner account could not be created: Missing required attribute(s): cost_center
[Medium  ] Attribute drift — CRM / E1003
           HR has ['department', 'title'] = ['Finance', 'Senior Financial Analyst'], app still has ['Accounting', 'Financial Analyst']; update failed: API 400: attribute 'costCenter' rejected by target schema
[Medium  ] Provisioning failure (create) — Payroll / E1002
           Joiner account could not be created: Missing required attribute(s): cost_center

========================================================================
RECONCILIATION SUMMARY
========================================================================
Application           HR active  App active     Status
Email Suite                   5           6   MISMATCH
CRM                           5           4   MISMATCH
Payroll                       5           5   MISMATCH

Findings by severity: Critical=1, High=1, Medium=4, Low=0
Orphaned active accounts (leavers still able to sign in): 2
```

Note the Payroll row: the counts match (5 = 5) yet the status is MISMATCH, because one joiner was never created and one leaver was never deprovisioned. Count-based reconciliation alone would have passed this application.

## Design notes and limitations

- Failures are **data-driven**: each target app declares `simulated_failures` (action + employee_id + error), which mirrors how a real connector surfaces per-object errors in a sync log.
- Severity model: an orphaned account is Critical if the account is privileged, High otherwise; create failures and drift are Medium. This is a deliberately simple model. Production IGA tools layer in entitlement criticality, dormancy, and last-login data.
- The simulator models SCIM semantics (create/update/deactivate via `active=false`) but does not implement the SCIM HTTP protocol, filtering, or pagination; group/entitlement provisioning is out of scope.
- Sample data is entirely fictional. Any resemblance to real people or organisations is coincidental.

## License

MIT: see `LICENSE`.
