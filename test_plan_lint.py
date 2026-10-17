import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import plan_lint


def plan(steps, **extra):
    p = {"steps": steps}
    p.update(extra)
    return p


def codes(findings):
    return [f["code"] for f in findings]


class StructuralTests(unittest.TestCase):
    def test_clean_plan_has_no_findings(self):
        p = plan([
            {"id": "a", "tool": "search"},
            {"id": "b", "tool": "summarize", "needs": ["a"],
             "inputs": {"text": {"from": "a"}}},
        ], outputs=["b"])
        self.assertEqual(plan_lint.lint_plan(p), [])

    def test_single_step_is_not_dead_end(self):
        p = plan([{"id": "only", "tool": "t"}])
        self.assertEqual(plan_lint.lint_plan(p), [])

    def test_duplicate_id(self):
        p = plan([{"id": "a", "tool": "t"}, {"id": "a", "tool": "t"}])
        self.assertIn("duplicate-id", codes(plan_lint.lint_plan(p)))

    def test_missing_id_and_tool(self):
        p = plan([{"tool": "t"}, {"id": "b"}])
        c = codes(plan_lint.lint_plan(p))
        self.assertIn("missing-id", c)
        self.assertIn("missing-tool", c)

    def test_dangling_needs(self):
        p = plan([{"id": "a", "tool": "t", "needs": ["ghost"]}])
        self.assertIn("dangling-needs", codes(plan_lint.lint_plan(p)))

    def test_self_dependency(self):
        p = plan([{"id": "a", "tool": "t", "needs": ["a"]}])
        c = codes(plan_lint.lint_plan(p))
        self.assertIn("self-dependency", c)
        self.assertIn("dependency-cycle", c)

    def test_two_node_cycle(self):
        p = plan([
            {"id": "a", "tool": "t", "needs": ["b"]},
            {"id": "b", "tool": "t", "needs": ["a"]},
        ])
        c = codes(plan_lint.lint_plan(p))
        self.assertIn("dependency-cycle", c)

    def test_diamond_is_not_a_cycle(self):
        p = plan([
            {"id": "a", "tool": "t"},
            {"id": "b", "tool": "t", "needs": ["a"]},
            {"id": "c", "tool": "t", "needs": ["a"]},
            {"id": "d", "tool": "t", "needs": ["b", "c"]},
        ])
        self.assertNotIn("dependency-cycle", codes(plan_lint.lint_plan(p)))

    def test_dead_end_warning_not_error(self):
        p = plan([
            {"id": "a", "tool": "t"},
            {"id": "b", "tool": "t", "needs": ["a"]},
            {"id": "c", "tool": "t"},
        ], outputs=["b"])
        f = plan_lint.lint_plan(p)
        dead = [x for x in f if x["code"] == "dead-end"]
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0]["severity"], plan_lint.WARNING)
        self.assertEqual(dead[0]["step"], "c")
        self.assertFalse(plan_lint.has_errors(f))

    def test_input_binding_unknown_and_self(self):
        p = plan([
            {"id": "a", "tool": "t",
             "inputs": {"x": {"from": "ghost"}, "y": {"from": "a"}}},
        ])
        c = codes(plan_lint.lint_plan(p))
        self.assertIn("dangling-binding", c)
        self.assertIn("self-binding", c)

    def test_binding_against_downstream_step_is_cycle(self):
        # a needs nothing, but binds b's result while b needs a: impossible.
        p = plan([
            {"id": "a", "tool": "t", "inputs": {"x": {"from": "b"}}},
            {"id": "b", "tool": "t", "needs": ["a"]},
        ])
        c = codes(plan_lint.lint_plan(p))
        self.assertIn("binding-cycle", c)

    def test_allowed_tools_enforced(self):
        p = plan([{"id": "a", "tool": "shell"}])
        f = plan_lint.lint_plan(p, allowed_tools={"search"})
        self.assertIn("unknown-tool", codes(f))
        self.assertEqual(plan_lint.lint_plan(p, allowed_tools={"shell"}), [])

    def test_findings_are_deterministically_ordered(self):
        p = plan([
            {"id": "z", "tool": "t", "needs": ["ghost"]},
            {"id": "a", "tool": "t"},
            {"id": "a", "tool": "t"},
        ], outputs=["z"])
        f1 = codes(plan_lint.lint_plan(p))
        f2 = codes(plan_lint.lint_plan(p))
        self.assertEqual(f1, f2)
        self.assertEqual(f1, sorted(set(f1), key=f1.index))


class InputCapTests(unittest.TestCase):
    def test_oversize_plan_rejected(self):
        big = json.dumps(plan([{"id": f"s{i}", "tool": "t"} for i in range(200_000)]))
        with self.assertRaises(plan_lint.InvalidInput):
            plan_lint.load_plan(big)

    def test_deep_nesting_rejected(self):
        deep = json.dumps({"steps": [], "x": json.loads("[" * 100 + "]" * 100)})
        with self.assertRaises(plan_lint.InvalidInput):
            plan_lint.load_plan(deep)

    def test_non_object_plan_rejected(self):
        with self.assertRaises(plan_lint.InvalidInput):
            plan_lint.load_plan("[1,2,3]")

    def test_too_many_steps_rejected(self):
        p = plan([{"id": str(i), "tool": "t"} for i in range(plan_lint.MAX_STEPS + 1)])
        with self.assertRaises(plan_lint.InvalidInput):
            plan_lint.load_plan(json.dumps(p))


class OracleTests(unittest.TestCase):
    """Exhaustive cycle-detection oracle: every directed graph on 3 nodes
    (64 edge subsets) checked against an independent brute-force reachability
    oracle that does not share code with the linter's Kahn pass."""

    @staticmethod
    def oracle_has_cycle(nodes, edges):
        # Independent brute force: repeated squaring-free BFS from each node.
        reach = {n: set() for n in nodes}
        for a, b in edges:
            reach[a].add(b)
        changed = True
        while changed:
            changed = False
            for a in nodes:
                new = set()
                for b in reach[a]:
                    new |= reach[b]
                if not new <= reach[a]:
                    reach[a] |= new
                    changed = True
        return any(n in reach[n] for n in nodes)

    def test_all_3_node_graphs(self):
        nodes = ["a", "b", "c"]
        all_edges = [(x, y) for x in nodes for y in nodes if x != y]
        checked = 0
        for r in range(len(all_edges) + 1):
            for combo in itertools.combinations(all_edges, r):
                edges = set(combo)
                steps = [{"id": n, "tool": "t",
                          "needs": sorted(b for a, b in edges if a == n)}
                         for n in nodes]
                findings = plan_lint.lint_plan(plan(steps))
                has_cycle = "dependency-cycle" in codes(findings)
                self.assertEqual(has_cycle, self.oracle_has_cycle(nodes, edges),
                                 f"mismatch on edges {edges}")
                checked += 1
        self.assertEqual(checked, 2 ** len(all_edges))  # all 64 graphs


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(Path(__file__).parent / "plan_lint.py"), *args],
                              capture_output=True, text=True)

    def test_cli_clean_exit_0(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(plan([{"id": "a", "tool": "t"}]), fh)
            path = fh.name
        r = self.run_cli(path)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_cli_errors_exit_1(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(plan([{"id": "a", "tool": "t", "needs": ["ghost"]}]), fh)
            path = fh.name
        r = self.run_cli(path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("dangling-needs", r.stdout)

    def test_cli_invalid_input_exit_2(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json")
            path = fh.name
        r = self.run_cli(path)
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid input", r.stderr)

    def test_cli_exact_1mib_boundary(self):
        # Build a valid plan whose serialized size is exactly 1 MiB.
        base = plan([{"id": "a", "tool": "t", "inputs": {"pad": ""}}])
        text = json.dumps(base)
        pad_len = plan_lint.MAX_BYTES - len(text)
        base["steps"][0]["inputs"]["pad"] = "x" * pad_len
        text = json.dumps(base)
        self.assertEqual(len(text.encode()), plan_lint.MAX_BYTES)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(text)
            path = fh.name
        r = self.run_cli(path)
        self.assertEqual(r.returncode, 0, r.stderr)
        # One byte over the cap must be exit 2.
        base["steps"][0]["inputs"]["pad"] += "x"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(base, fh)
            path = fh.name
        r = self.run_cli(path)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
