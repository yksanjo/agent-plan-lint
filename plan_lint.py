"""agent-plan-lint: offline static linter for agent execution plans.

A plan is a JSON object:
  {
    "name": "optional plan name",
    "steps": [
      {
        "id": "fetch",                 # required, unique string
        "tool": "web.fetch",           # required, non-empty string
        "needs": ["other-step-id"],    # optional list of step ids
        "inputs": {"url": "...", "raw": {"from": "fetch"}}
                                       # optional object; a value of the
                                       # form {"from": "<step-id>"} binds
                                       # another step's result as input
      }
    ]
  }

The linter is purely structural. It never executes anything, never calls a
model, and makes no claim that a clean plan will succeed at runtime.
"""

import json
import sys

MAX_BYTES = 1 << 20          # 1 MiB plan size cap
MAX_DEPTH = 64               # JSON nesting cap
MAX_STEPS = 10_000           # step count cap

ERROR = "error"
WARNING = "warning"


class InvalidInput(Exception):
    """Raised when the plan file cannot be parsed within the safety caps."""


def _check_depth(node, depth=0):
    if depth > MAX_DEPTH:
        raise InvalidInput(f"JSON nesting deeper than {MAX_DEPTH}")
    if isinstance(node, dict):
        for v in node.values():
            _check_depth(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _check_depth(v, depth + 1)


def load_plan(text):
    """Parse plan JSON text, enforcing size and depth caps."""
    if len(text.encode("utf-8", "replace")) > MAX_BYTES:
        raise InvalidInput(f"plan larger than {MAX_BYTES} bytes")
    try:
        plan = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as e:
        raise InvalidInput(f"invalid JSON: {e}")
    _check_depth(plan)
    if not isinstance(plan, dict):
        raise InvalidInput("plan must be a JSON object")
    steps = plan.get("steps")
    if not isinstance(steps, list):
        raise InvalidInput('plan must contain a "steps" list')
    if len(steps) > MAX_STEPS:
        raise InvalidInput(f"more than {MAX_STEPS} steps")
    return plan


def lint_plan(plan, allowed_tools=None):
    """Return a deterministically ordered list of findings.

    Each finding is a dict: {severity, code, step, message}. `step` is the
    step id the finding belongs to, or None for plan-level findings.
    """
    findings = []

    def add(sev, code, step, msg):
        findings.append({"severity": sev, "code": code, "step": step,
                         "message": msg})

    steps = plan.get("steps", [])
    ids = []
    seen = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            add(ERROR, "step-not-object", None, f"steps[{i}] is not an object")
            continue
        sid = step.get("id")
        if not isinstance(sid, str) or not sid:
            add(ERROR, "missing-id", None, f"steps[{i}] has no non-empty string id")
            continue
        if sid in seen:
            add(ERROR, "duplicate-id", sid, f"duplicate step id {sid!r}")
        else:
            seen.add(sid)
            ids.append(sid)
        tool = step.get("tool")
        if not isinstance(tool, str) or not tool:
            add(ERROR, "missing-tool", sid, f"step {sid!r} has no non-empty string tool")
        elif allowed_tools is not None and tool not in allowed_tools:
            add(ERROR, "unknown-tool", sid,
                f"step {sid!r} uses tool {tool!r}, not in the allowed list")

    known = set(ids)
    needs_map = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        sid = step.get("id")
        if not isinstance(sid, str) or not sid or sid not in ids:
            continue
        needs = step.get("needs", [])
        if not isinstance(needs, list) or any(not isinstance(n, str) for n in needs):
            add(ERROR, "bad-needs", sid, f"step {sid!r} needs must be a list of step ids")
            needs = []
        for n in needs:
            if n == sid:
                add(ERROR, "self-dependency", sid, f"step {sid!r} needs itself")
            elif n not in known:
                add(ERROR, "dangling-needs", sid,
                    f"step {sid!r} needs unknown step {n!r}")
        needs_map[sid] = [n for n in needs if n in known]

    for step in steps:
        if not isinstance(step, dict):
            continue
        sid = step.get("id")
        if not isinstance(sid, str) or not sid or sid not in ids:
            continue
        inputs = step.get("inputs", {})
        if inputs is not None and not isinstance(inputs, dict):
            add(ERROR, "bad-inputs", sid, f"step {sid!r} inputs must be an object")
            continue
        for key, val in (inputs or {}).items():
            if isinstance(val, dict) and set(val.keys()) == {"from"}:
                ref = val["from"]
                if not isinstance(ref, str):
                    add(ERROR, "bad-binding", sid,
                        f"step {sid!r} input {key!r} binding is not a step id")
                elif ref == sid:
                    add(ERROR, "self-binding", sid,
                        f"step {sid!r} input {key!r} binds its own result")
                elif ref not in known:
                    add(ERROR, "dangling-binding", sid,
                        f"step {sid!r} input {key!r} binds unknown step {ref!r}")
                elif _reachable(ref, sid, needs_map):
                    add(ERROR, "binding-cycle", sid,
                        f"step {sid!r} input {key!r} binds {ref!r}, which depends on {sid!r}")

    # Cycle detection over needs edges (Kahn's algorithm).
    indegree = {sid: 0 for sid in ids}
    for sid, reqs in needs_map.items():
        for n in set(reqs):
            indegree[sid] += 1
    ready = sorted(s for s, d in indegree.items() if d == 0)
    done = set()
    queue = list(ready)
    while queue:
        cur = queue.pop(0)
        if cur in done:
            continue
        done.add(cur)
        for sid in sorted(needs_map):
            if cur in needs_map[sid]:
                indegree[sid] -= needs_map[sid].count(cur)
                if indegree[sid] == 0:
                    queue.append(sid)
    stuck = sorted(set(ids) - done)
    if stuck:
        add(ERROR, "dependency-cycle", None,
            "dependency cycle among steps: " + ", ".join(repr(s) for s in stuck))

    # Dead-end warning: with a declared "outputs" list, any sink step not
    # listed there produces output nothing consumes. Without declared outputs
    # we cannot tell intentional sinks from accidental ones, so we stay quiet.
    outputs = plan.get("outputs")
    if outputs is not None:
        if not isinstance(outputs, list) or any(not isinstance(o, str) for o in outputs):
            add(ERROR, "bad-outputs", None, 'plan "outputs" must be a list of step ids')
            outputs = []
        for o in outputs:
            if o not in known:
                add(ERROR, "dangling-output", None, f'plan output {o!r} is not a step id')
        needed = set()
        bound = set()
        for reqs in needs_map.values():
            needed.update(reqs)
        for step in steps:
            if isinstance(step, dict):
                sinputs = step.get("inputs")
                if isinstance(sinputs, dict):
                    for val in sinputs.values():
                        if isinstance(val, dict) and set(val.keys()) == {"from"} and isinstance(val["from"], str):
                            bound.add(val["from"])
        declared = set(outputs)
        for sid in ids:
            if sid not in needed and sid not in bound and sid not in declared:
                add(WARNING, "dead-end", sid,
                    f"step {sid!r} result is not used and not declared an output")

    order = {ERROR: 0, WARNING: 1}
    findings.sort(key=lambda f: (order[f["severity"]], f["code"],
                                 f["step"] or "", f["message"]))
    return findings


def _reachable(src, dst, needs_map):
    """True if dst is reachable from src by following needs edges."""
    stack = [src]
    seen = set()
    while stack:
        cur = stack.pop()
        if cur == dst:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(needs_map.get(cur, []))
    return False


def has_errors(findings):
    return any(f["severity"] == ERROR for f in findings)


def main(argv):
    if len(argv) < 2 or len(argv) > 3:
        print("usage: plan_lint.py PLAN.json [allowed-tool,another-tool]",
              file=sys.stderr)
        return 2
    allowed = None
    if len(argv) == 3:
        allowed = set(t for t in argv[2].split(",") if t)
    try:
        with open(argv[1], "rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise InvalidInput(f"plan larger than {MAX_BYTES} bytes")
        plan = load_plan(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, InvalidInput) as e:
        print(f"invalid input: {e}", file=sys.stderr)
        return 2
    findings = lint_plan(plan, allowed)
    for f in findings:
        where = f" [{f['step']}]" if f["step"] else ""
        print(f"{f['severity']}: {f['code']}{where}: {f['message']}")
    if has_errors(findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
