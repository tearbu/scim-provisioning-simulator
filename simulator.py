#!/usr/bin/env python3
"""SCIM 2.0 provisioning lifecycle simulator.

Simulates a user provisioning run between an HR source-of-truth roster and
multiple target applications, SCIM-style: joiners are created, movers are
updated, and leavers are deprovisioned. The simulator deliberately models
real-world failure modes — target-app API errors and missing required
attributes — and reports the consequences that matter in an identity
governance review: orphaned active accounts for leavers, attribute drift,
and duplicate accounts.

All data is fictional. Run with --demo to execute against the bundled
sample data, or point --hr / --apps at your own JSON files in the same
format.

Standard library only. No network access is performed; target "API calls"
are simulated and failures are driven by the sample/configuration data.
"""

import argparse
import json
import sys
from pathlib import Path

SEVERITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# Simulated SCIM client
# --------------------------------------------------------------------------

class SimulatedApp:
    """A target application whose SCIM endpoint behaviour is simulated.

    Failures are data-driven: the app's configuration may declare
    simulated_failures, each naming an action ("create", "update",
    "deprovision") and an employee_id for which the call fails with the
    given error — mirroring how a real connector surfaces API errors.
    """

    def __init__(self, config):
        self.name = config["name"]
        self.required_attributes = config.get("required_attributes", [])
        self.accounts = {a["employee_id"]: dict(a) for a in config.get("accounts", [])}
        self.failures = {(f["action"], f["employee_id"]): f["error"]
                         for f in config.get("simulated_failures", [])}

    def _maybe_fail(self, action, employee_id):
        return self.failures.get((action, employee_id))

    def create(self, user):
        err = self._maybe_fail("create", user["employee_id"])
        if err:
            return False, err
        missing = [a for a in self.required_attributes if not user.get(a)]
        if missing:
            return False, "Missing required attribute(s): " + ", ".join(missing)
        self.accounts[user["employee_id"]] = {
            "employee_id": user["employee_id"],
            "user_name": user["email"],
            "department": user["department"],
            "title": user["title"],
            "active": True,
            "privileged": user.get("privileged", False),
        }
        return True, "201 Created"

    def update(self, user):
        err = self._maybe_fail("update", user["employee_id"])
        if err:
            return False, err
        missing = [a for a in self.required_attributes if not user.get(a)]
        if missing:
            return False, "Missing required attribute(s): " + ", ".join(missing)
        acct = self.accounts[user["employee_id"]]
        acct["department"] = user["department"]
        acct["title"] = user["title"]
        return True, "200 OK"

    def deprovision(self, employee_id):
        err = self._maybe_fail("deprovision", employee_id)
        if err:
            return False, err
        self.accounts[employee_id]["active"] = False
        return True, "200 OK (active=false)"


# --------------------------------------------------------------------------
# Provisioning run
# --------------------------------------------------------------------------

def run_app(app, roster):
    """Run one provisioning cycle for a single application.

    Returns (events, findings, active_ids): log lines, finding dicts
    (severity, type, app, employee_id, detail), and the set of
    employee_ids whose accounts are active in the app after the run.
    """
    events, findings = [], []
    by_id = {u["employee_id"]: u for u in roster}

    # --- Joiners & movers -------------------------------------------------
    for user in roster:
        eid = user["employee_id"]
        if user["status"] == "active":
            acct = app.accounts.get(eid)
            if acct is None:
                ok, msg = app.create(user)
                if ok:
                    events.append(f"CREATE   {eid} -> {app.name}: {msg}")
                else:
                    events.append(f"CREATE   {eid} -> {app.name}: FAILED ({msg})")
                    findings.append({
                        "severity": "Medium", "type": "Provisioning failure (create)",
                        "app": app.name, "employee_id": eid,
                        "detail": f"Joiner account could not be created: {msg}"})
            elif acct["active"]:
                drift = [f for f in ("department", "title")
                         if acct.get(f) != user.get(f)]
                if drift:
                    ok, msg = app.update(user)
                    if ok:
                        events.append(
                            f"UPDATE   {eid} -> {app.name}: {msg} "
                            f"(synced {', '.join(drift)})")
                    else:
                        events.append(f"UPDATE   {eid} -> {app.name}: FAILED ({msg})")
                        findings.append({
                            "severity": "Medium", "type": "Attribute drift",
                            "app": app.name, "employee_id": eid,
                            "detail": (f"HR has {drift} = "
                                       f"{[user[f] for f in drift]}, app still has "
                                       f"{[acct[f] for f in drift]}; update failed: {msg}")})

    # --- Leavers -----------------------------------------------------------
    for eid, acct in sorted(app.accounts.items()):
        user = by_id.get(eid)
        is_leaver = (user is None) or user["status"] == "terminated"
        if is_leaver and acct["active"]:
            ok, msg = app.deprovision(eid)
            if ok:
                events.append(f"DEPROV  {eid} -> {app.name}: {msg}")
            else:
                events.append(f"DEPROV  {eid} -> {app.name}: FAILED ({msg})")
                sev = "Critical" if acct.get("privileged") else "High"
                findings.append({
                    "severity": sev, "type": "Orphaned active account",
                    "app": app.name, "employee_id": eid,
                    "detail": (f"Leaver's account is still ACTIVE because "
                               f"deprovisioning failed: {msg}"
                               + (" Account has privileged access."
                                  if acct.get("privileged") else ""))})
    active_ids = {eid for eid, a in app.accounts.items() if a["active"]}
    return events, findings, active_ids


def find_duplicates(app_configs):
    """Duplicates are declared in the app configuration the same way a
    reconciliation export would reveal them: two account records (distinct
    user_names) bound to the same employee."""
    findings = []
    for cfg in app_configs:
        for dup in cfg.get("duplicate_accounts", []):
            findings.append({
                "severity": "Medium", "type": "Duplicate account",
                "app": cfg["name"], "employee_id": dup["employee_id"],
                "detail": (f"Two active accounts for one employee: "
                           f"{dup['user_names'][0]} and {dup['user_names'][1]}")})
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--demo", action="store_true",
                        help="run against the bundled fictional sample data")
    parser.add_argument("--hr", help="path to HR roster JSON")
    parser.add_argument("--apps", help="path to target apps JSON")
    args = parser.parse_args(argv)

    if args.demo:
        base = Path(__file__).parent / "sample_data"
        hr_path, apps_path = base / "hr_roster.json", base / "apps.json"
    elif args.hr and args.apps:
        hr_path, apps_path = Path(args.hr), Path(args.apps)
    else:
        parser.error("use --demo or provide both --hr and --apps")

    roster = load_json(hr_path)
    app_configs = load_json(apps_path)

    active_hr = [u for u in roster if u["status"] == "active"]
    leavers = [u for u in roster if u["status"] == "terminated"]

    print("=" * 72)
    print("SCIM PROVISIONING SIMULATION — SYNC RUN REPORT")
    print("=" * 72)
    print(f"HR roster: {len(roster)} identities "
          f"({len(active_hr)} active, {len(leavers)} terminated)")
    print(f"Target applications: {len(app_configs)}")
    print()

    all_findings = find_duplicates(app_configs)
    reconciliation = []

    hr_active_ids = {u["employee_id"] for u in active_hr}
    for cfg in app_configs:
        app = SimulatedApp(cfg)
        events, findings, active_ids = run_app(app, roster)
        all_findings.extend(findings)

        print(f"--- {app.name} " + "-" * max(0, 66 - len(app.name)))
        for line in events:
            print(f"  {line}")
        if not events:
            print("  (no changes required)")
        print(f"  Result: {len(active_ids)} active account(s) after sync")
        print()
        reconciliation.append((app.name, len(active_hr), len(active_ids),
                               active_ids == hr_active_ids))

    # --- Findings -----------------------------------------------------------
    print("=" * 72)
    print("FINDINGS")
    print("=" * 72)
    if all_findings:
        for f in sorted(all_findings,
                         key=lambda x: (SEVERITY_ORDER[x["severity"]], x["app"])):
            print(f"[{f['severity']:<8}] {f['type']} — {f['app']} / {f['employee_id']}")
            print(f"           {f['detail']}")
    else:
        print("No findings.")
    print()

    # --- Reconciliation -------------------------------------------------------
    print("=" * 72)
    print("RECONCILIATION SUMMARY")
    print("=" * 72)
    print(f"{'Application':<20} {'HR active':>10} {'App active':>11} {'Status':>10}")
    for name, hr_n, app_n, same_identities in reconciliation:
        # Counts alone can hide errors (a failed create plus a failed
        # deprovision cancel out), so identity sets are compared too.
        status = "IN SYNC" if same_identities else "MISMATCH"
        print(f"{name:<20} {hr_n:>10} {app_n:>11} {status:>10}")
    sev_counts = {}
    for f in all_findings:
        sev_counts[f["severity"]] = sev_counts.get(f["severity"], 0) + 1
    print()
    print("Findings by severity: " + ", ".join(
        f"{s}={sev_counts.get(s, 0)}"
        for s in ("Critical", "High", "Medium", "Low")))
    orphans = sum(1 for f in all_findings if f["type"] == "Orphaned active account")
    print(f"Orphaned active accounts (leavers still able to sign in): {orphans}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
