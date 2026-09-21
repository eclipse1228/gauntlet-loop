from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from devin_gauntlet.common import (Blocked, atomic, git, load, object_hash, parse_result,
                                   real_file, relative_path, validate_judgment, validate_plan)
from devin_gauntlet.engine import Engine, initialize


def unit(ident="score", *, mode="ab", reference="input:0", path="score.txt", dependencies=None):
    return {"id": ident, "goal": "Exceed the real reference integer", "bar": "The greater integer wins",
            "rules": [], "depends_on": dependencies or [], "mode": mode,
            "reference_files": [reference] if mode == "ab" else [], "prepare": [], "capture": [],
            "checks": [] if mode == "ab" else [[sys.executable, "-c", "import sys;sys.exit(0)"]],
            "artifacts": [path], "demo_path": path}


class ProtocolTests(unittest.TestCase):
    def test_bare_pass_rejected(self):
        with self.assertRaises(Blocked):
            parse_result("VERDICT: PASS", "new")

    def test_stale_envelope_rejected(self):
        with self.assertRaises(Blocked):
            parse_result('<GAUNTLET_RESULT:old>{}</GAUNTLET_RESULT>', "new")

    def test_valid_envelope(self):
        self.assertEqual(parse_result('noise\n<GAUNTLET_RESULT:x>{"ok":true}</GAUNTLET_RESULT>', "x"), {"ok": True})

    def test_duplicate_envelope_rejected(self):
        with self.assertRaises(Blocked):
            parse_result('<GAUNTLET_RESULT:x>{}</GAUNTLET_RESULT>' * 2, "x")

    def test_malformed_json_rejected(self):
        with self.assertRaises(Blocked):
            parse_result('<GAUNTLET_RESULT:x>{oops}</GAUNTLET_RESULT>', "x")

    def test_cycle_rejected(self):
        a, b = unit("a", dependencies=["b"]), unit("b", dependencies=["a"])
        with self.assertRaises(Blocked):
            validate_plan({"tasks": [a, b]})

    def test_unknown_dependency_rejected(self):
        with self.assertRaises(Blocked):
            validate_plan({"tasks": [unit(dependencies=["missing"])]})

    def test_duplicate_task_rejected(self):
        with self.assertRaises(Blocked):
            validate_plan({"tasks": [unit(), unit()]})

    def test_measurement_needs_executable_check(self):
        t = unit(mode="measurement")
        t["checks"] = []
        with self.assertRaises(Blocked):
            validate_plan({"tasks": [t]})

    def test_reference_pairs_required(self):
        t = unit()
        t["reference_files"] = []
        with self.assertRaises(Blocked):
            validate_plan({"tasks": [t]})

    def test_shell_string_is_not_argv(self):
        t = unit()
        t["capture"] = ["echo hello > screenshot.png"]
        with self.assertRaises(Blocked):
            validate_plan({"tasks": [t]})

    def test_paths_cannot_traverse_or_use_history(self):
        for p in ("../outside", "/etc/passwd", ".git/logs/HEAD", ".gauntlet-runtime/report.md"):
            with self.subTest(path=p), self.assertRaises(Blocked):
                relative_path(p)

    def test_evidence_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "real").write_text("1")
            (root / "fake").symlink_to(root / "real")
            with self.assertRaises(Blocked):
                real_file(root, "fake")

    def test_empty_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "empty").touch()
            with self.assertRaises(Blocked):
                real_file(root, "empty")

    def packet(self):
        packet = {"packet_id": "current", "mode": "measurement", "files": ["checks/x.txt"]}
        result = {"packet_id": "current", "verdict": "PASS", "biggest_gap": "",
                  "observations": [{"artifact": "checks/x.txt", "detail": "actual log"}]}
        return packet, result

    def test_wrong_packet_id_rejected(self):
        packet, result = self.packet()
        result["packet_id"] = "old"
        with self.assertRaises(Blocked):
            validate_judgment(result, packet, None, True)

    def test_missing_observation_rejected(self):
        packet, result = self.packet()
        result["observations"] = []
        with self.assertRaises(Blocked):
            validate_judgment(result, packet, None, True)

    def test_invented_artifact_rejected(self):
        packet, result = self.packet()
        result["observations"][0]["artifact"] = "nonexistent"
        with self.assertRaises(Blocked):
            validate_judgment(result, packet, None, True)

    def test_nonzero_check_overrides_critic_pass(self):
        packet, result = self.packet()
        passed, gap = validate_judgment(result, packet, None, False)
        self.assertFalse(passed)
        self.assertIn("verification failed", gap)

    def test_ab_tie_is_not_a_win(self):
        packet, result = self.packet()
        packet["mode"] = "ab"
        result.update(winner="TIE", biggest_gap="equal")
        self.assertFalse(validate_judgment(result, packet, "A", True)[0])

    def test_unjudgeable_blocks_instead_of_passing(self):
        packet, result = self.packet()
        result.update(verdict="UNJUDGEABLE", biggest_gap="Cannot inspect")
        with self.assertRaises(Blocked):
            validate_judgment(result, packet, None, True)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "original"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet")
        for name in ("score.txt", "left.txt", "right.txt"):
            (self.repo / name).write_text("0\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "--quiet", "-m", "initial")
        self.ref = self.base / "reference.txt"
        self.ref.write_text("1\n")
        self.run = self.base / "run"
        self.fixture = self.base / "fixture.json"
        self.child_procs = []

    def tearDown(self):
        for p in self.child_procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        self.temp.cleanup()

    def setup_run(self, tasks=None, **fake_settings):
        initialize(self.run, self.repo, "Meet actual independently judged bars", [self.ref])
        atomic(self.fixture, {"plan": {"tasks": tasks or [unit()]}, **fake_settings})
        config = load(self.run / "config.json")
        config["agent_command"] = [sys.executable, str(ROOT / "examples/fake_devin.py"), "--fixture", str(self.fixture)]
        atomic(self.run / "config.json", config)

    def invoke(self, *args, timeout=40):
        result = subprocess.run([sys.executable, "-m", "devin_gauntlet", *args], cwd=ROOT,
                                capture_output=True, text=True, timeout=timeout)
        return result

    def spawn(self, *args):
        p = subprocess.Popen([sys.executable, "-m", "devin_gauntlet", *args], cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.child_procs.append(p)
        return p

    def wait_for(self, predicate, timeout=10):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.03)
        self.fail("Timed out waiting for condition")

    def traces(self):
        path = self.base / "trace.jsonl"
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text().splitlines() if x]

    def test_seven_rounds_builder_stop_text_ignored_and_source_untouched(self):
        self.ref.write_text("6\n")
        self.setup_run()
        result = self.invoke("run", str(self.run), "--allow-execution")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = load(self.run / "state.json")
        self.assertEqual(state["status"], "BAR_MET")
        self.assertEqual(state["tasks"]["score"]["round"], 7)
        self.assertEqual((self.repo / "score.txt").read_text().strip(), "0")
        self.assertEqual((self.run / "work/integration/score.txt").read_text().strip(), "7")
        self.assertTrue((self.run / "result.patch").stat().st_size)
        self.assertEqual(git(self.repo, "apply", "--check", str(self.run / "result.patch"), check=False).returncode, 0)
        critics = [r for r in self.traces() if r["event"] == "start" and r["role"] == "critic"]
        self.assertEqual(len(critics), 8)  # seven task gates and same-revision final check
        self.assertEqual(len({r["pid"] for r in critics}), 8)
        self.assertEqual(len({r["cwd"] for r in critics}), 8)
        self.assertTrue(all("--resume" not in r["argv"] and "--continue" not in r["argv"] for r in critics))

    def test_parallel_workstreams_and_integration_recheck(self):
        self.setup_run([unit("left", path="left.txt"), unit("right", path="right.txt")], builder_sleep=0.15)
        result = self.invoke("run", str(self.run), "--allow-execution")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        traces = self.traces()
        starts = [x for x in traces if x["role"] == "builder" and x["event"] == "start"]
        ends = {x["pid"]: x["time"] for x in traces if x["role"] == "builder" and x["event"] == "end"}
        self.assertTrue(any(a["pid"] != b["pid"] and a["time"] < b["time"] < ends[a["pid"]]
                            for a in starts for b in starts))
        self.assertTrue(all(load(self.run / "state.json")["tasks"][x]["status"] == "INTEGRATED" for x in ("left", "right")))
        events = [json.loads(x) for x in (self.run / "private/events.jsonl").read_text().splitlines()]
        finals = [x for x in events if x["event"] == "judgment" and x["final"]]
        self.assertEqual(len({x["revision"] for x in finals}), 1)

    def test_dependency_runs_after_upstream_integration(self):
        self.setup_run([unit("left", path="left.txt"), unit("right", path="right.txt", dependencies=["left"])])
        result = self.invoke("run", str(self.run), "--allow-execution")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = [json.loads(x) for x in (self.run / "private/events.jsonl").read_text().splitlines()]
        left_integrated = next(i for i, e in enumerate(events) if e["event"] == "integrated" and e["task"] == "left")
        right_built = next(i for i, e in enumerate(events) if e["event"] == "builder_round" and e["task"] == "right")
        self.assertLess(left_integrated, right_built)

    def test_final_regression_reopens_failed_workstream(self):
        a, b = unit("left", path="left.txt"), unit("right", path="right.txt", dependencies=["left"])
        b["demo_reset"] = ["left.txt"]
        self.setup_run([a, b])
        result = self.invoke("run", str(self.run), "--allow-execution")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = load(self.run / "state.json")
        self.assertGreaterEqual(state["wave"], 2)
        self.assertGreater(state["tasks"]["left"]["round"], 2)
        self.assertEqual((self.run / "work/integration/left.txt").read_text().strip(), "2")

    def test_malformed_critic_blocks_and_resume_retries(self):
        self.setup_run(critic_mode="bare_pass")
        result = self.invoke("run", str(self.run), "--allow-execution")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(load(self.run / "state.json")["status"], "BLOCKED")
        fixture = load(self.fixture)
        fixture["critic_mode"] = "normal"
        atomic(self.fixture, fixture)
        resumed = self.invoke("resume", str(self.run), "--allow-execution")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)

    def test_stop_terminates_managed_process_group_and_resume(self):
        self.setup_run(builder_sleep=30, spawn_child=True)
        p = self.spawn("run", str(self.run), "--allow-execution")
        self.wait_for(lambda: any(x["event"] == "child" for x in self.traces()))
        child = next(x["child_pid"] for x in self.traces() if x["event"] == "child")
        stopped = self.invoke("stop", str(self.run))
        self.assertEqual(stopped.returncode, 0)
        self.assertEqual(p.wait(timeout=10), 130)
        self.assertEqual(load(self.run / "state.json")["status"], "USER_STOPPED")
        self.assertEqual(load(self.run / "private/active.json"), {})
        stat = Path(f"/proc/{child}/stat")
        self.assertTrue(not stat.exists() or stat.read_text().split(")", 1)[1].split()[0] == "Z")
        fixture = load(self.fixture)
        fixture.update(builder_sleep=0, spawn_child=False)
        atomic(self.fixture, fixture)
        result = self.invoke("resume", str(self.run), "--allow-execution")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wall_limit_is_not_success(self):
        self.setup_run(builder_sleep=20)
        result = self.invoke("run", str(self.run), "--allow-execution", "--max-seconds", "0.6")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(load(self.run / "state.json")["status"], "USER_LIMIT_REACHED")

    def test_frozen_plan_tamper_is_blocked(self):
        self.setup_run()
        self.assertEqual(self.invoke("plan", str(self.run), "--allow-execution").returncode, 0)
        plan = load(self.run / "plan.json")
        plan["tasks"][0]["bar"] = "Everything automatically passes"
        atomic(self.run / "plan.json", plan)
        result = self.invoke("run", str(self.run), "--allow-execution")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Plan was changed", result.stdout)

    def test_frozen_reference_tamper_is_blocked(self):
        self.setup_run()
        self.assertEqual(self.invoke("plan", str(self.run), "--allow-execution").returncode, 0)
        asset = load(self.run / "plan.json")["tasks"][0]["frozen_references"][0]["path"]
        (self.run / asset).write_text("0")
        result = self.invoke("run", str(self.run), "--allow-execution")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Frozen reference was changed", result.stdout)

    def test_goal_change_requires_new_run(self):
        self.setup_run()
        self.assertEqual(self.invoke("plan", str(self.run), "--allow-execution").returncode, 0)
        cfg = load(self.run / "config.json")
        cfg["goal"] = "A different goal"
        atomic(self.run / "config.json", cfg)
        result = self.invoke("run", str(self.run), "--allow-execution")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Goal/project/rules changed", result.stdout)

    def test_execution_requires_explicit_authorization(self):
        self.setup_run()
        result = self.invoke("run", str(self.run))
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.base / "trace.jsonl").exists())

    def test_critic_payload_has_no_builder_history_or_ab_map(self):
        self.setup_run()
        result = self.invoke("run", str(self.run), "--allow-execution")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        evaluations = list((self.run / "private/evaluations").glob("*.json"))
        packet = next(load(x)["packet"] for x in evaluations if "packet" in load(x))
        self.assertEqual(set(packet), {"packet_id", "mode", "goal", "bar", "rules", "files"})
        self.assertNotIn("candidate_label", json.dumps(packet))
        self.assertNotIn("previous_critic_gap", json.dumps(packet))
        self.assertTrue(all(x.startswith(("A/", "B/")) for x in packet["files"]))

    def test_measurement_uses_actual_runner_check_and_repeats(self):
        t = unit(mode="measurement")
        t["checks"] = [[sys.executable, "-c", "from pathlib import Path; import sys; n=int(Path('score.txt').read_text()); print(n); sys.exit(0 if n>=3 else 1)"]]
        self.setup_run([t], critic_mode="lying_pass")
        result = self.invoke("run", str(self.run), "--allow-execution")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = load(self.run / "state.json")
        self.assertEqual(state["tasks"]["score"]["round"], 3)

    def test_optional_smoother_is_write_role_and_followed_by_rechecks(self):
        self.setup_run()
        result = self.invoke("run", str(self.run), "--allow-execution", "--smoothing")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        starts = [x for x in self.traces() if x["event"] == "start"]
        idx = next(i for i, x in enumerate(starts) if x["role"] == "smoother")
        self.assertTrue(any(x["role"] == "critic" for x in starts[idx + 1:]))
        self.assertIn("--sandbox", starts[idx]["argv"])

    def test_resume_flags_disallowed_in_backend(self):
        self.setup_run()
        cfg = load(self.run / "config.json")
        cfg["agent_command"] += ["--continue"]
        atomic(self.run / "config.json", cfg)
        with self.assertRaises(Blocked):
            Engine(self.run)

    def test_duplicate_runner_lock(self):
        self.setup_run(builder_sleep=30)
        p = self.spawn("run", str(self.run), "--allow-execution")
        self.wait_for(lambda: any(x["role"] == "builder" for x in self.traces()))
        second = self.invoke("run", str(self.run), "--allow-execution")
        self.assertEqual(second.returncode, 2)
        self.assertIn("Another runner", second.stderr)
        self.invoke("stop", str(self.run))
        p.wait(timeout=10)

    def test_fresh_source_capture_creates_real_artifact(self):
        t = unit()
        t["capture"] = [[sys.executable, "-c", "import os; from pathlib import Path; Path(os.environ['GAUNTLET_OUTPUT_DIR'],'actual.txt').write_bytes(Path('score.txt').read_bytes())"]]
        t["artifacts"] = ["@output/actual.txt"]
        self.setup_run([t])
        result = self.invoke("run", str(self.run), "--allow-execution")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        evidence = list((self.run / "public/evidence").rglob("artifact-001.txt"))
        self.assertTrue(evidence)


if __name__ == "__main__":
    unittest.main(verbosity=2)
