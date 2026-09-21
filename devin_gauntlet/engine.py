from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import fcntl
import html
import json
import os
import secrets
import shutil
import signal
import tempfile
import uuid
from pathlib import Path
from typing import Any

from . import prompts
from .common import (Blocked, Stopped, atomic, clone, commit, digest, git, head, load,
                     object_hash, real_file, render_progress, validate_judgment, validate_plan)
from .runtime import Runtime


def initialize(root: Path, project: Path, goal: str, references: list[Path],
               *, parallel: int = 2, model: str | None = None) -> None:
    root, project = root.resolve(), project.resolve()
    if not goal.strip() or parallel < 1:
        raise Blocked("A goal and a positive parallel limit are required")
    if root.is_relative_to(project) or project.is_relative_to(root):
        raise Blocked("Run directory and original repository must not contain each other")
    if root.exists() and any(root.iterdir()):
        raise Blocked("Run directory is not empty")
    if git(project, "rev-parse", "--show-toplevel").stdout.strip() != str(project):
        raise Blocked("--project must be the repository root")
    if git(project, "status", "--porcelain").stdout.strip():
        raise Blocked("Commit/stash original changes first; the harness only snapshots committed code")
    original_head = head(project)
    if git(project, "ls-files", ".gauntlet-runtime").stdout.strip():
        raise Blocked("The repository uses the reserved .gauntlet-runtime path")
    for ref in references:
        if ref.is_symlink() or not ref.is_file() or ref.stat().st_size == 0:
            raise Blocked("Reference must be a nonempty regular file: " + str(ref))
    root.mkdir(parents=True, exist_ok=True)
    refs = []
    for i, ref in enumerate(references, 1):
        dst = root / "references/inbox" / f"input-{i:03d}{ref.suffix.lower()}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ref, dst)
        refs.append(str(dst))
    config = {"version": 1, "project_dir": str(project), "goal": goal,
              "references": refs, "rules": [], "agent_command": ["devin"],
              "models": {r: model for r in ("lead", "builder", "critic", "smoother")},
              "max_parallel": parallel, "call_timeout_seconds": None}
    atomic(root / "config.json", config)
    state = {"version": 1, "status": "CREATED", "base_commit": original_head,
             "wave": 0, "tasks": {}, "error": "", "active_seconds": 0.0}
    atomic(root / "state.json", state)
    clone(project, root / "work/integration", original_head)
    render_progress(root, state)


class Engine:
    def __init__(self, root: Path, *, max_seconds: float | None = None):
        self.root = root.resolve()
        self.config = load(self.root / "config.json")
        self.state = load(self.root / "state.json")
        self.integration = self.root / "work/integration"
        parallel = self.config.get("max_parallel", 2)
        if isinstance(parallel, bool) or not isinstance(parallel, int) or parallel < 1:
            raise Blocked("max_parallel must be a positive integer")
        timeout = self.config.get("call_timeout_seconds")
        if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
            raise Blocked("call_timeout_seconds must be null or positive")
        argv = self.config.get("agent_command")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise Blocked("agent_command must be a nonempty argv array")
        if any(x in {"--continue", "-c", "--resume", "-r"} or x.startswith("--resume=") for x in argv[1:]):
            raise Blocked("agent_command must not resume an existing conversation")
        self.rt = Runtime(self.root, self.config, self.event, max_seconds=max_seconds)
        self.merge_lock = asyncio.Lock()
        self.plan: dict | None = None

    @contextlib.contextmanager
    def lock(self):
        with (self.root / "runner.lock").open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Blocked("Another runner owns this run directory") from exc
            # Read the checkpoint under the lock, not from a pre-lock snapshot.
            self.state = load(self.root / "state.json")
            # On hard termination a child may survive the supervisor. Do not
            # silently start another writer or kill a possibly reused PID.
            active_file = self.root / "private/active.json"
            for old in (load(active_file).values() if active_file.exists() else []):
                try:
                    os.kill(old["pid"], 0)
                except ProcessLookupError:
                    continue
                raise Blocked(f"Recorded process {old['pid']} is still alive. Inspect/stop it before resuming.")
            atomic(active_file, {})
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def save(self) -> None:
        atomic(self.root / "state.json", self.state)
        render_progress(self.root, self.state)

    def event(self, kind: str, **data: Any) -> None:
        path = self.root / "private/events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), "event": kind, **data}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.rt.stop_event.set)

    def config_basis(self) -> str:
        return object_hash({k: self.config.get(k) for k in ("project_dir", "goal", "rules")})

    def verify_frozen(self) -> None:
        if self.state.get("config_basis_hash") != self.config_basis():
            raise Blocked("Goal/project/rules changed after planning; create a new run")
        if self.plan is None or object_hash(load(self.root / "plan.json")) != self.state.get("plan_hash"):
            raise Blocked("Plan was changed after it was frozen. Create a new run for a different bar.")
        for t in self.plan["tasks"]:
            for asset in t.get("frozen_references", []):
                p = self.root / asset["path"]
                if not p.is_file() or p.is_symlink() or digest(p) != asset["sha256"]:
                    raise Blocked("Frozen reference was changed: " + str(p))

    async def prepare_plan(self) -> None:
        self.signals()
        lead = self.root / "work/lead"
        if not lead.exists():
            clone(self.integration, lead)
        self.state.update(status="PLANNING", error="")
        self.save()
        result = await self.rt.agent("lead", lead, prompts.LEAD, {
            "goal": self.config["goal"], "references": self.config["references"],
            "rules": self.config.get("rules", []),
        })
        plan = validate_plan(result)
        for t in plan["tasks"]:
            frozen = []
            for i, name in enumerate(t["reference_files"], 1):
                p = Path(name)
                if not p.is_absolute():
                    p = lead / p
                p = p.absolute()
                # The lead may create new references in its own checkout, or
                # select explicitly supplied files. No arbitrary host-file harvest.
                approved = {Path(x).resolve() for x in self.config["references"]}
                if p.is_symlink() or not (p.resolve().is_relative_to(lead.resolve()) or p.resolve() in approved):
                    raise Blocked("Reference outside the lead/input-reference workspace: " + str(p))
                if not p.is_file() or p.stat().st_size == 0:
                    raise Blocked("Lead proposed a nonexistent/empty reference: " + str(p))
                target = self.root / "references/frozen" / t["id"] / f"ref-{i:03d}{p.suffix.lower()}"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, target)
                frozen.append({"path": target.relative_to(self.root).as_posix(), "sha256": digest(target)})
            # Absolute source names and original references never enter critic input.
            t["frozen_references"] = frozen
            t.pop("reference_files", None)
        self.plan = plan
        atomic(self.root / "plan.json", plan)
        self.state.update(status="PLAN_READY", plan_hash=object_hash(plan),
                          progress_title=plan.get("progress_title", "Devin Gauntlet"), error="",
                          config_basis_hash=self.config_basis())
        self.state["tasks"] = {t["id"]: {"status": "PENDING", "round": 0, "feedback": ""}
                               for t in plan["tasks"]}
        self.save()
        self.event("plan_frozen", sha256=self.state["plan_hash"])

    async def grouped(self, awaitables):
        tasks = [asyncio.create_task(a) for a in awaitables]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def hooks(self, task: dict, source: Path, output: Path, phase: str) -> list[dict]:
        logs = []
        for argv in task[phase]:
            expanded = [s.replace("{workspace}", str(source)).replace("{output}", str(output)) for s in argv]
            result = await self.rt.process(expanded, source, f"{phase}:{task['id']}",
                                           env_extra={"GAUNTLET_OUTPUT_DIR": str(output)})
            logs.append({"argv": expanded, **result})
            if result["returncode"] and phase != "checks":
                break
        return logs

    async def evaluate(self, task: dict, source_repo: Path, revision: str, *, final: bool = False) -> tuple[bool, str]:
        self.verify_frozen()
        self.rt.check_stop()
        evaluation_id = uuid.uuid4().hex
        work = self.root / "work/evaluations" / evaluation_id
        source = work / "source"
        clone(source_repo, source, revision)
        output = work / "output"
        output.mkdir()
        # The critic is not launched inside the source repository or run log tree.
        packet_dir = Path(tempfile.mkdtemp(prefix="gauntlet-critic-"))
        try:
            preparation = await self.hooks(task, source, output, "prepare")
            if any(x["returncode"] for x in preparation):
                return False, "Evaluation setup failed:\n" + self.log_summary(preparation)
            captured = await self.hooks(task, source, output, "capture")
            checks = await self.hooks(task, source, output, "checks")
            if any(x["returncode"] for x in captured):
                return False, "Actual artifact capture failed:\n" + self.log_summary(captured)
            artifacts: list[Path] = []
            for path in task["artifacts"]:
                try:
                    artifacts.append(real_file(output, path[8:]) if path.startswith("@output/") else real_file(source, path))
                except Blocked as exc:
                    return False, str(exc)
            # Evidence collection must not silently change the committed artifact.
            # Generated untracked outputs are allowed; tracked source edits are not.
            if head(source) != revision or git(source, "diff", "HEAD", "--name-only").stdout.strip():
                return False, "Capture/check commands modified tracked source. Evaluate the committed product without changing it."
            candidate_label: str | None = None
            if task["mode"] == "ab":
                candidate_label = secrets.choice(("A", "B"))
                reference_label = "B" if candidate_label == "A" else "A"
                for i, (candidate, frozen) in enumerate(zip(artifacts, task["frozen_references"]), 1):
                    reference = self.root / frozen["path"]
                    # Do not label bytes with an incorrect extension.
                    for label, src in ((candidate_label, candidate), (reference_label, reference)):
                        dst = packet_dir / label / f"artifact-{i:03d}{src.suffix.lower()}"
                        dst.parent.mkdir(exist_ok=True)
                        shutil.copyfile(src, dst)
            else:
                for i, src in enumerate(artifacts, 1):
                    dst = packet_dir / "artifact" / f"artifact-{i:03d}{src.suffix.lower()}"
                    dst.parent.mkdir(exist_ok=True)
                    shutil.copyfile(src, dst)
                for i, check in enumerate(checks, 1):
                    path = packet_dir / "checks" / f"check-{i:03d}.txt"
                    path.parent.mkdir(exist_ok=True)
                    # This is raw runner-captured output, not the builder's account.
                    atomic(path, self.log_summary([check], limit=None), text=True)
            files = sorted(p.relative_to(packet_dir).as_posix() for p in packet_dir.rglob("*") if p.is_file())
            if not files:
                raise Blocked("Evaluation has no actual evidence")
            hashes = {p: digest(packet_dir / p) for p in files}
            packet = {"packet_id": evaluation_id, "mode": task["mode"], "goal": task["goal"],
                      "bar": task["bar"], "rules": self.config.get("rules", []) + task["rules"], "files": files}
            atomic(self.root / "private/evaluations" / (evaluation_id + ".json"), {
                "revision": revision, "task": task["id"], "candidate_label": candidate_label,
                "sha256": hashes, "packet": packet, "final": final,
            })
            result = await self.rt.agent("critic", packet_dir, prompts.CRITIC, packet)
            if any(not (packet_dir / p).is_file() or digest(packet_dir / p) != h for p, h in hashes.items()):
                raise Blocked("Evidence bytes were changed during criticism")
            # Ensure original references also remained unchanged throughout the call.
            self.verify_frozen()
            passed, gap = validate_judgment(result, packet, candidate_label,
                                            checks_ok=all(x["returncode"] == 0 for x in checks))
            if any(x["returncode"] for x in checks):
                gap += "\n" + self.log_summary(checks)
            atomic(self.root / "private/evaluations" / (evaluation_id + ".result.json"), result)
            self.publish_evidence(task["id"], evaluation_id, packet_dir, files, revision, passed, gap)
            self.event("judgment", task=task["id"], packet=evaluation_id, passed=passed,
                       revision=revision, final=final)
            return passed, gap
        finally:
            # Evidence/public snapshots and logs survive. Disposable source copies do not.
            shutil.rmtree(packet_dir, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def log_summary(logs: list[dict], limit: int | None = 6000) -> str:
        text = "\n\n".join("ARGV: " + json.dumps(x["argv"]) + f"\nEXIT: {x['returncode']}\nSTDOUT:\n" +
                             x["stdout"] + "\nSTDERR:\n" + x["stderr"] for x in logs)
        return text if limit is None else text[-limit:]

    def publish_evidence(self, ident: str, evaluation_id: str, packet_dir: Path,
                         files: list[str], revision: str, passed: bool, gap: str) -> None:
        target = self.root / "public/evidence" / evaluation_id
        links = []
        for name in files:
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(packet_dir / name, dest)
            link = html.escape(name, quote=True)
            if dest.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                links.append(f'<h3>{link}</h3><img style="max-width:100%" src="{link}">')
            else:
                links.append(f'<p><a href="{link}">{link}</a></p>')
        page = "<!doctype html><meta charset='utf-8'><h1>" + html.escape(ident) + "</h1>"
        page += f"<p>Revision {revision} · {'PASS' if passed else 'FAIL'}</p><pre>{html.escape(gap)}</pre>"
        atomic(target / "index.html", page + "".join(links), text=True)
        self.state["tasks"][ident]["evidence_page"] = f"evidence/{evaluation_id}/index.html"
        self.save()

    async def build_until_pass(self, task: dict, done: dict[str, asyncio.Event]) -> None:
        ident = task["id"]
        record = self.state["tasks"][ident]
        for dep in task["depends_on"]:
            await done[dep].wait()
        if record["status"] == "INTEGRATED":
            done[ident].set()
            return
        workspace = self.root / "work/builders" / ident
        async with self.merge_lock:
            if not workspace.exists():
                clone(self.integration, workspace)
        # No max_passes and no model-controlled break. Only a validated judgment,
        # successful integration, an explicit stop, or an operational exception exits.
        while True:
            self.rt.check_stop()
            self.verify_frozen()
            # Preserve partial edits. If interrupted during a conflict, let the
            # builder resolve it before asking git to commit or merge again.
            pending_merge = (workspace / ".git/MERGE_HEAD").exists()
            if not pending_merge:
                commit(workspace, "Checkpoint before synchronization")
            async with self.merge_lock:
                expected_integration = head(self.integration)
                git(workspace, "fetch", "--quiet", str(self.integration), expected_integration)
                if pending_merge:
                    conflict_text = "Resolve the interrupted merge in this workspace."
                else:
                    merge = git(workspace, "merge", "--no-edit", "FETCH_HEAD", check=False)
                    if merge.returncode and not (workspace / ".git/MERGE_HEAD").exists():
                        raise Blocked("Cannot synchronize builder: " + merge.stderr)
                    conflict_text = merge.stdout if merge.returncode else ""
            record.update(status="BUILDING", round=record["round"] + 1)
            self.save()
            self.event("builder_round", task=ident, round=record["round"])
            payload = {"goal": self.config["goal"], "task": task,
                       "reference_files": [str(self.root / r["path"]) for r in task["frozen_references"]],
                       "previous_critic_gap": record.get("feedback", ""),
                       "rules": self.config.get("rules", []),
                       "merge_conflicts": conflict_text}
            # Deliberately ignore the builder's response content. Even "PASS" or
            # "stop now" cannot change the state transition below.
            await self.rt.agent("builder", workspace, prompts.BUILDER, payload, structured=False)
            revision = commit(workspace, "Gauntlet candidate " + ident)
            record.update(status="EVALUATING", candidate_commit=revision)
            self.save()
            passed, gap = await self.evaluate(task, workspace, revision)
            record["feedback"] = gap
            self.save()
            if not passed:
                continue
            async with self.merge_lock:
                # A pass on an obsolete integrated base cannot be merged blindly.
                if (head(self.integration) != expected_integration or
                        git(workspace, "merge-base", "--is-ancestor", expected_integration, revision, check=False).returncode):
                    record["feedback"] = "Other workstreams changed the integrated artifact. Synchronize, inspect, and judge again."
                    self.save()
                    continue
                git(self.integration, "fetch", "--quiet", str(workspace), revision)
                git(self.integration, "merge", "--ff-only", "FETCH_HEAD")
                record.update(status="INTEGRATED", integrated_commit=revision)
                self.save()
                self.event("integrated", task=ident, revision=revision)
                done[ident].set()
                return

    async def run(self, *, smoothing: bool = False) -> str:
        self.signals()
        self.plan = load(self.root / "plan.json")
        self.verify_frozen()
        self.state.update(status="RUNNING", error="")
        self.save()
        while True:
            self.rt.check_stop()
            self.state["wave"] += 1
            self.save()
            done = {t["id"]: asyncio.Event() for t in self.plan["tasks"]}
            for ident, rec in self.state["tasks"].items():
                if rec["status"] == "INTEGRATED":
                    done[ident].set()
            await self.grouped(self.build_until_pass(t, done) for t in self.plan["tasks"])
            if smoothing:
                self.state["status"] = "SMOOTHING"
                self.save()
                await self.rt.agent("smoother", self.integration, prompts.SMOOTHER, {
                    "goal": self.config["goal"], "rules": self.config.get("rules", []),
                    "bars": [{"goal": t["goal"], "bar": t["bar"]} for t in self.plan["tasks"]],
                }, structured=False)
                commit(self.integration, "Optional smoothing")
            self.state["status"] = "FINAL_RECHECK"
            self.save()
            revision = head(self.integration)
            # Every gate must hold on THE SAME integrated revision. These are the
            # existing workstream bars, not a newly invented quality scoring rubric.
            results = await self.grouped(self.evaluate(t, self.integration, revision, final=True)
                                         for t in self.plan["tasks"])
            self.rt.check_stop()
            if all(passed for passed, gap in results):
                self.state.update(status="BAR_MET", final_commit=revision)
                self.export_patch()
                self.save()
                return "BAR_MET"
            for task, (passed, gap) in zip(self.plan["tasks"], results):
                if not passed:
                    self.state["tasks"][task["id"]].update(status="PENDING", feedback=gap)
            self.state["status"] = "RUNNING"
            self.save()

    def export_patch(self) -> Path:
        patch = self.root / "result.patch"
        # Binary output is preserved; do not decode/re-encode a binary git patch.
        import subprocess
        with patch.open("wb") as out:
            result = subprocess.run(["git", "-C", str(self.integration), "diff", "--binary",
                                     self.state["base_commit"], "HEAD"], stdout=out,
                                    stderr=subprocess.PIPE, timeout=120)
        if result.returncode:
            raise Blocked(result.stderr.decode(errors="replace"))
        return patch
