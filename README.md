# agent-plan-lint

Offline static linter for agent execution plans.

Agent frameworks increasingly hand an LLM's proposed plan - a list of steps, their tools, and their dependencies - to an executor. Before anything runs, someone should check the plan is even structurally sound. That is all this tool does. It reads a JSON plan, checks the structure, and reports findings. It never executes a step, never calls a model, and never touches the network.

## Plan format

```json
{
  "name": "research-brief",
  "outputs": ["brief"],
  "steps": [
    {"id": "search", "tool": "web.search"},
    {"id": "fetch", "tool": "web.fetch", "needs": ["search"],
     "inputs": {"url": {"from": "search"}}},
    {"id": "brief", "tool": "text.summarize", "needs": ["fetch"],
     "inputs": {"text": {"from": "fetch"}}}
  ]
}
```

- `steps[].id` - required, unique string.
- `steps[].tool` - required, non-empty string.
- `steps[].needs` - optional list of step ids that must complete first.
- `steps[].inputs` - optional object. A value of exactly `{"from": "<id>"}` binds another step's result as this input.
- `outputs` - optional list of step ids whose results are the plan's intended outputs. Declaring it turns on dead-end detection.

## What it checks

| Code | Severity | Meaning |
| --- | --- | --- |
| `missing-id` / `missing-tool` | error | step lacks a required field |
| `duplicate-id` | error | two steps share an id |
| `dangling-needs` | error | `needs` references an unknown step |
| `self-dependency` | error | a step needs itself |
| `dependency-cycle` | error | the needs graph has a cycle (Kahn's algorithm) |
| `bad-needs` / `bad-inputs` / `bad-binding` / `bad-outputs` | error | wrong type in a structural field |
| `dangling-binding` | error | `{"from": id}` references an unknown step |
| `self-binding` | error | a step binds its own result as input |
| `binding-cycle` | error | a binding points at a step that depends on the binder |
| `unknown-tool` | error | tool not in the caller-supplied allowed list |
| `dangling-output` | error | declared output is not a step id |
| `dead-end` | warning | sink step not declared in `outputs` (only checked when `outputs` is declared) |

Findings are sorted deterministically (severity, code, step, message), so the same plan always produces the same report.

## Use

```sh
python3 plan_lint.py plan.json                      # lint
python3 plan_lint.py plan.json web.search,web.fetch # also enforce allowed tools
python3 demo.py                                     # synthetic demo
python3 -m unittest test_plan_lint                  # 22 tests
```

Exit codes: `0` clean (warnings may still print), `1` errors found, `2` invalid input.

## Safety caps

The CLI refuses plans over 1 MiB, JSON nested deeper than 64 levels, and plans with more than 10,000 steps - all as exit 2, never a crash.

## Tests

22 tests, including an exhaustive independent oracle: all 64 directed graphs on 3 nodes are checked for cycle detection against a brute-force transitive-closure implementation that shares no code with the linter's Kahn pass, plus the exact 1 MiB accept/reject boundary.

## Limits

- **Structure only.** A clean plan can still fail at runtime: tools can be misconfigured, inputs can be wrong, models can produce nonsense. This tool says nothing about whether a plan is a good idea.
- **No semantics.** It does not know what any tool does. The allowed-tools list is a string check, not a capability or permission model.
- **Bindings are a convention.** It understands only the `{"from": "id"}` convention shown above; other wiring notations are ignored.
- **Dead-end detection is opt-in.** Without a declared `outputs` list it stays silent rather than guess which sinks are intentional.
- **Trusted-input API.** The size/depth/step caps are enforced by the CLI and by `load_plan`; calling `lint_plan` directly with a hand-built object bypasses the input caps.

## License

MIT - see [LICENSE](LICENSE).
