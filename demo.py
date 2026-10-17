"""Synthetic demo: lint one clean plan and one broken plan.

All plans here are made up. No model calls, no network, nothing executes.
"""
import json
import plan_lint

clean = {
    "name": "research-brief",
    "outputs": ["brief"],
    "steps": [
        {"id": "search", "tool": "web.search"},
        {"id": "fetch", "tool": "web.fetch", "needs": ["search"],
         "inputs": {"url": {"from": "search"}}},
        {"id": "brief", "tool": "text.summarize", "needs": ["fetch"],
         "inputs": {"text": {"from": "fetch"}}},
    ],
}

broken = {
    "name": "daily-report",
    "outputs": ["email"],
    "steps": [
        {"id": "collect", "tool": "db.query"},
        {"id": "collect", "tool": "db.query"},                    # duplicate id
        {"id": "crunch", "tool": "stats.run", "needs": ["missing"]},  # dangling
        {"id": "email", "tool": "mail.send",
         "inputs": {"body": {"from": "email"}}},                  # self-binding
        {"id": "archive", "tool": "fs.write"},                    # dead end
    ],
}

for label, p in [("clean", clean), ("broken", broken)]:
    findings = plan_lint.lint_plan(p, allowed_tools={
        "web.search", "web.fetch", "text.summarize",
        "db.query", "stats.run", "mail.send", "fs.write"})
    print(f"--- {label} plan: {p['name']} ---")
    if not findings:
        print("no findings")
    for f in findings:
        where = f" [{f['step']}]" if f["step"] else ""
        print(f"{f['severity']}: {f['code']}{where}: {f['message']}")
    print()
