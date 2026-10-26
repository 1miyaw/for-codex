"""Offline behavioral tests; set OMICS_TEST_IMAGE for an actual Docker smoke test."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import agent
import evaluate
import team


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.spec, self.manifest = agent.load_input(Path(__file__).with_name("example.json"))
        self.config = {"demo": True, "max_revisions": 1, "max_turns": 8}
        self.addCleanup(self.temp.cleanup)

    def fixture(self):
        return json.loads(agent.demo_response("generate", "", {"input": self.spec}))["tasks"][0]

    def test_demo_resume_and_no_extra_handoff(self):
        packet = agent.run(self.spec, self.manifest, self.root, agent.demo_team, self.config)
        self.assertEqual(packet["tasks"][0]["status"], "demo_only")
        self.assertFalse(packet["benchmark_ready"])
        self.assertEqual(set(p.name for p in self.root.iterdir()), {"state.json", "tasks.json", "tasks.csv"})
        def forbidden(*args):
            self.fail("resume must not call model")
        again = agent.run(self.spec, self.manifest, self.root, forbidden, self.config)
        self.assertEqual(packet, again)
        state = json.loads((self.root / "state.json").read_text())
        self.assertEqual({r["role"] for r in state["calls"].values()},
                         {"literature", "data", "designer", "reviewer"})

    def test_input_change_invalidates_checkpoint(self):
        agent.run(self.spec, self.manifest, self.root, agent.demo_team, self.config)
        with self.assertRaisesRegex(ValueError, "changed"):
            agent.run(dict(self.spec, title="changed"), self.manifest, self.root, agent.demo_team, self.config)

    def test_tool_denied_then_recovery(self):
        seen, record = [], {}
        def client(stage, system, payload):
            seen.append(payload)
            if not payload["history"]:
                return '{"action":"tool","name":"read_gold","arguments":{}}'
            return '{"action":"final","result":{"status":"insufficient_data"}}'
        result = team.Agent("solver", client).run("s", "", {}, record, lambda: None)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertFalse(seen[-1]["history"][0]["observation"]["ok"])

    def test_tool_result_is_cached(self):
        calls = []
        tool = team.Tool("read", lambda: calls.append(1) or {"x": 1})
        def client(stage, system, payload):
            return ('{"action":"tool","name":"read","arguments":{}}' if not payload["history"]
                    else '{"action":"final","result":{"done":true}}')
        record = {}
        actor = team.Agent("data", client, {"read": tool})
        actor.run("d", "", {}, record, lambda: None)
        actor.run("d", "", {}, record, lambda: None)
        self.assertEqual(calls, [1])

    def test_invalid_json_retained(self):
        record = {}
        with self.assertRaises(json.JSONDecodeError):
            team.Agent("designer", lambda *a: "broken").run("g", "", {}, record, lambda: None)
        self.assertEqual(record["turns"][0]["raw"], "broken")

    def test_turn_budget_stops_loop(self):
        record = {}
        client = lambda *a: '{"action":"tool","name":"unknown","arguments":{}}'
        with self.assertRaisesRegex(RuntimeError, "budget"):
            team.Agent("solver", client, max_turns=2).run("s", "", {}, record, lambda: None)
        self.assertEqual(len(record["turns"]), 2)

    def test_interrupted_tool_is_not_replayed(self):
        record = {}
        class Stop(Exception):
            pass
        def crash():
            raise Stop()
        client = lambda *a: '{"action":"tool","name":"crash","arguments":{}}'
        actor = team.Agent("solver", client, {"crash": team.Tool("crash", crash)})
        with self.assertRaises(Stop):
            actor.run("s", "", {}, record, lambda: None)
        with self.assertRaisesRegex(RuntimeError, "interrupted tool"):
            actor.run("s", "", {}, record, lambda: None)

    def test_blind_packet_omits_private_fields_and_host_paths(self):
        task = self.fixture()
        task["reference_answer"] = "SECRET_GOLD"
        task["milestones"][0]["acceptance"] = "SECRET_GOLD"
        task["milestones"][0]["decision"] = "SECRET_GOLD"
        manifest = {k: dict(v, path="/private/SECRET_GOLD.csv") for k, v in self.manifest.items()}
        public = team.solver_packet(task, self.spec, manifest)
        serialized = json.dumps(public)
        self.assertNotIn("SECRET_GOLD", serialized)
        self.assertNotIn("sources", serialized)
        self.assertNotIn("review", serialized)
        self.assertTrue(public["data"][0]["path"].startswith("/data/input_"))

    def test_csv_inspection_and_tool_id_boundary(self):
        path = self.root / "data.csv"
        path.write_text("donor,condition\nA,treated\nB,control\n")
        tools = team.data_tools({"D": {"available": True, "path": str(path)}})
        self.assertEqual(tools["inspect_data"].run(data_id="D")["columns"], ["donor", "condition"])
        with self.assertRaises(ValueError):
            tools["inspect_data"].run(data_id="../../secret")

    def test_audit_cannot_claim_uninspected_data(self):
        audit = {"datasets": [{"data_id": k, "status": "missing", "observed": [],
                               "limitations": []} for k in self.manifest]}
        with self.assertRaisesRegex(ValueError, "inspect"):
            team.validate_audit(audit, self.manifest, {"turns": []})

    def test_fabricated_source_quote_rejected(self):
        extraction = json.loads(agent.demo_response("extract", "", {"input": self.spec}))
        extraction["evidence"][0]["quote"] = "This was never reported."
        with self.assertRaisesRegex(ValueError, "exact"):
            agent.validate_evidence(extraction, self.spec)

    def test_cycle_and_artifact_traversal_rejected(self):
        extraction = json.loads(agent.demo_response("extract", "", {"input": self.spec}))
        evidence = agent.validate_evidence(extraction, self.spec)
        draft = {"tasks": [self.fixture()], "gaps": []}
        draft["tasks"][0]["milestones"][0]["depends_on"] = ["M3"]
        with self.assertRaises(ValueError):
            agent.validate_tasks(draft, self.spec, evidence)
        draft["tasks"][0] = self.fixture()
        draft["tasks"][0]["milestones"][0]["artifact"] = "../gold.csv"
        with self.assertRaises(ValueError):
            agent.validate_tasks(draft, self.spec, evidence)

    def test_contradictory_grading_rejected(self):
        with self.assertRaises(ValueError):
            evaluate.validate_grade({"verdict": "supported", "execution_supported": True,
                                     "data_dependence_supported": False, "feedback": "contradiction"})

    def test_container_mounts_only_selected_files(self):
        runner = evaluate.DockerRunner("sha256:" + "a" * 64,
                    [(str(self.root / "selected.csv"), "/data/input_0.csv")], self.root / "work")
        command = runner.command("test")
        self.assertIn("--network=none", command)
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=ALL", command)
        mounts = [command[i + 1] for i, x in enumerate(command) if x == "--mount"]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(mounts[1].endswith(",readonly"))
        self.assertNotIn("OPENAI_API_KEY", " ".join(command))

    def test_symlink_artifacts_rejected(self):
        runner = evaluate.DockerRunner("unused", [], self.root / "work")
        (self.root / "secret").write_text("private")
        (runner.work / "output.csv").symlink_to(self.root / "secret")
        with self.assertRaises(ValueError):
            runner.artifact("output.csv")

    def test_execution_control_and_cached_artifacts(self):
        task = self.fixture()
        received, bundles, started = [], {}, set()
        class FakeRunner:
            def __init__(self, image, files, work, timeout):
                self.work, self.files, self.receipts = Path(work), files, []
            def tools(self):
                def execute(code):
                    for milestone in task["milestones"]:
                        (self.work / milestone["artifact"]).write_text("observed data result")
                    self.receipts.append({"exit_code": 0, "timed_out": False, "output_truncated": False})
                    return {"exit_code": 0}
                return {"run_python": team.Tool("fake", execute)}
            def artifact(self, name):
                value = (self.work / name).read_bytes()
                return {"name": name, "bytes": len(value),
                        "sha256": hashlib.sha256(value).hexdigest(), "preview": value.decode()}
        def step(name, role, system, payload, tools=None):
            received.append((name, role, payload))
            started.add(name)
            if name.startswith("grade"):
                return {"verdict": "supported", "execution_supported": True,
                        "data_dependence_supported": True, "feedback": "test fixture"}
            if name.endswith("without_data"):
                return {"status": "insufficient_data", "answer": "no data", "artifacts": []}
            tools["run_python"].run(code="fixture")
            return {"status": "completed", "answer": "result",
                    "artifacts": [m["artifact"] for m in task["milestones"]]}
        step.bundle = bundles.get
        step.started = lambda n: n in started
        step.artifact_dir = lambda n: self.root / hashlib.sha256(n.encode()).hexdigest()[:16]
        def capture(name, fn):
            if name not in bundles:
                bundles[name] = fn()
            return bundles[name]
        step.capture = capture
        result = evaluate.verify(task, self.spec, self.manifest, step, "fixture",
                                 runner_factory=FakeRunner)
        self.assertEqual(result["status"], "needs_human_review")
        self.assertTrue(result["execution_verified"])
        self.assertTrue(result["no_data_control_verified"])
        for name, role, payload in received:
            if role == "solver":
                self.assertNotIn("reference_answer", json.dumps(payload))
        received.clear()
        evaluate.verify(task, self.spec, self.manifest, step, "fixture", runner_factory=FakeRunner)
        self.assertEqual([role for _, role, _ in received], ["reviewer"])
        name = next(k for k, v in bundles.items() if v["artifacts"])
        first = bundles[name]["artifacts"][0]
        (step.artifact_dir(name) / first["name"]).write_text("tampered")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            evaluate.verify(task, self.spec, self.manifest, step, "fixture", runner_factory=FakeRunner)

    @unittest.skipUnless(os.environ.get("OMICS_TEST_IMAGE"), "optional real Docker check")
    def test_docker_executes_without_gold(self):
        image = evaluate.DockerRunner.resolve_image(os.environ["OMICS_TEST_IMAGE"])
        selected = self.root / "input.csv"
        selected.write_text("x\n3\n")
        (self.root / "gold.txt").write_text("SECRET")
        runner = evaluate.DockerRunner(image, [(str(selected), "/data/input_0.csv")],
                                       self.root / "work", timeout=30)
        result = runner.execute(
            "from pathlib import Path\n"
            "assert Path('/data/input_0.csv').read_text() == 'x\\n3\\n'\n"
            "assert not Path('/data/gold.txt').exists()\n"
            "Path('/work/result.txt').write_text('3')\nprint('ok')\n")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(runner.artifact("result.txt")["preview"], "3")


if __name__ == "__main__":
    unittest.main()
