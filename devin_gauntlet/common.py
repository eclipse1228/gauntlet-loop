from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any


class Blocked(RuntimeError):
    """An operational error, never a quality pass."""


class Stopped(RuntimeError):
    pass


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic(path: Path, value: Any, *, text: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    data = value if text else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with tmp.open("w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # Durability of the directory entry, where supported.
    if os.name == "posix":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def object_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
    # Do not execute global hooks, sign commits, or launch an editor.
    argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
            "-c", "user.name=Gauntlet Harness", "-c", "user.email=gauntlet@localhost",
            "-c", "core.editor=true", "-C", str(repo), *args]
    result = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120)
    if check and result.returncode:
        raise Blocked(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def clone(source: Path, target: Path, revision: str | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    git(target.parent, "clone", "--no-hardlinks", "--quiet", str(source), str(target))
    if revision:
        git(target, "checkout", "--detach", revision)
    exclude = target / ".git/info/exclude"
    with exclude.open("a", encoding="utf-8") as f:
        f.write("\n/.gauntlet-runtime/\n")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    diff = git(repo, "diff", "--cached", "--check", check=False)
    # Unresolved conflict markers are errors. Whitespace warnings are left to the bar.
    if "leftover conflict marker" in diff.stdout:
        raise Blocked("Unresolved conflict markers remain in " + str(repo))
    merging = (repo / ".git/MERGE_HEAD").exists()
    if merging or git(repo, "diff", "--cached", "--quiet", check=False).returncode:
        git(repo, "commit", "--quiet", "-m", message)
    return head(repo)


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise Blocked("Artifact paths must be nonempty POSIX relative paths")
    p = Path(value)
    if p.is_absolute() or ".." in p.parts or p == Path("."):
        raise Blocked("Unsafe relative path: " + value)
    if any(x in {".git", ".gauntlet-runtime", ".devin"} for x in p.parts):
        raise Blocked("An artifact cannot be a runtime/config/history file: " + value)
    return value


def real_file(root: Path, rel: str) -> Path:
    relative_path(rel)
    p = root / rel
    # Do not allow either a file or any parent to traverse a symlink.
    cur = p
    while cur != root:
        if cur.is_symlink():
            raise Blocked("Symlink evidence is not allowed: " + str(p))
        cur = cur.parent
    if not p.is_file() or p.stat().st_size == 0:
        raise Blocked("Missing or empty real artifact: " + str(p))
    if not p.resolve().is_relative_to(root.resolve()):
        raise Blocked("Artifact escapes root")
    return p


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Blocked(name + " must be a nonempty string")
    return value


def commands(value: Any, name: str) -> list[list[str]]:
    if not isinstance(value, list):
        raise Blocked(name + " must be a list of argv arrays")
    for argv in value:
        if not isinstance(argv, list) or not argv or not all(isinstance(s, str) and s for s in argv):
            raise Blocked(name + " contains an invalid argv array")
    return value


def validate_plan(value: Any) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list) or not value["tasks"]:
        raise Blocked("Lead must return a nonempty tasks array")
    plan = json.loads(json.dumps(value))
    ids: set[str] = set()
    for t in plan["tasks"]:
        if not isinstance(t, dict):
            raise Blocked("Each task must be an object")
        ident = t.get("id", "")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", ident) or ident in ids:
            raise Blocked("Invalid or duplicate task id")
        ids.add(ident)
        _text(t.get("goal"), "task.goal")
        _text(t.get("bar"), "task.bar")
        if t.get("mode") not in {"ab", "measurement"}:
            raise Blocked("mode must be ab or measurement")
        t.setdefault("depends_on", [])
        if not isinstance(t["depends_on"], list) or not all(isinstance(x, str) for x in t["depends_on"]):
            raise Blocked("depends_on must be a list of task IDs")
        t.setdefault("rules", [])
        if not isinstance(t["rules"], list) or not all(isinstance(x, str) for x in t["rules"]):
            raise Blocked("rules must be strings")
        for k in ("prepare", "capture", "checks"):
            t[k] = commands(t.get(k, []), k)
        t.setdefault("artifacts", [])
        if not isinstance(t["artifacts"], list):
            raise Blocked("artifacts must be an array")
        for p in t["artifacts"]:
            relative_path(p[8:] if isinstance(p, str) and p.startswith("@output/") else p)
        t.setdefault("reference_files", [])
        if not isinstance(t["reference_files"], list) or not all(isinstance(x, str) for x in t["reference_files"]):
            raise Blocked("reference_files must be paths")
        if t["mode"] == "ab" and (not t["artifacts"] or len(t["artifacts"]) != len(t["reference_files"])):
            raise Blocked("A/B requires an actual reference file for each artifact")
        if t["mode"] == "measurement" and not t["checks"]:
            raise Blocked("Measurement requires at least one executable check; prose is not a check")
    graph = {t["id"]: t["depends_on"] for t in plan["tasks"]}
    visiting: set[str] = set()
    visited: set[str] = set()
    def walk(n: str) -> None:
        if n not in ids:
            raise Blocked("Unknown dependency: " + n)
        if n in visiting:
            raise Blocked("Dependency cycle")
        if n in visited:
            return
        visiting.add(n)
        for dep in graph[n]:
            walk(dep)
        visiting.remove(n)
        visited.add(n)
    for ident in ids:
        walk(ident)
    return plan


def parse_result(output: str, nonce: str) -> dict:
    # A stray PASS, a builder's prose, or a prior invocation's response never counts.
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    start, end = f"<GAUNTLET_RESULT:{nonce}>", "</GAUNTLET_RESULT>"
    if output.count(start) != 1:
        raise Blocked("Missing or duplicate invocation-bound result envelope")
    body = output.split(start, 1)[1].split(end, 1)
    if len(body) != 2:
        raise Blocked("Unclosed result envelope")
    try:
        result = json.loads(body[0].strip())
    except json.JSONDecodeError as exc:
        raise Blocked("Invalid result JSON: " + str(exc)) from exc
    if not isinstance(result, dict):
        raise Blocked("Result must be an object")
    return result


def validate_judgment(result: dict, packet: dict, candidate_label: str | None,
                      checks_ok: bool) -> tuple[bool, str]:
    if result.get("packet_id") != packet["packet_id"]:
        raise Blocked("Critic returned a stale or wrong packet_id")
    observations = result.get("observations")
    if not isinstance(observations, list):
        raise Blocked("Critic omitted observations of actual artifacts")
    required = set(packet["files"])
    seen = set()
    for item in observations:
        if not isinstance(item, dict) or item.get("artifact") not in required:
            raise Blocked("Critic cited a nonexistent artifact")
        _text(item.get("detail"), "observation.detail")
        seen.add(item["artifact"])
    if seen != required:
        raise Blocked("Critic did not supply observations for every required artifact")
    gap = result.get("biggest_gap", "")
    if not isinstance(gap, str):
        raise Blocked("biggest_gap must be a string")
    if packet["mode"] == "ab":
        winner = result.get("winner")
        if winner not in {"A", "B", "TIE", "UNJUDGEABLE"}:
            raise Blocked("Invalid A/B winner")
        passed = winner == candidate_label
    else:
        verdict = result.get("verdict")
        if verdict not in {"PASS", "FAIL", "UNJUDGEABLE"}:
            raise Blocked("Invalid measurement verdict")
        passed = verdict == "PASS"
    if result.get("winner") == "UNJUDGEABLE" or result.get("verdict") == "UNJUDGEABLE":
        raise Blocked("Critic could not inspect/judge the packet: " + gap)
    if not checks_ok:
        passed = False
        gap = "Runner-executed verification failed. " + gap
    if not passed and not gap.strip():
        raise Blocked("A failed comparison must identify the biggest remaining gap")
    return passed, gap


def render_progress(root: Path, state: dict) -> None:
    esc = html.escape
    rows = []
    for ident, t in state.get("tasks", {}).items():
        link = t.get("evidence_page")
        evidence = f'<a href="{esc(link, quote=True)}">actual artifacts</a>' if link else "—"
        rows.append(f"<tr><td>{esc(ident)}</td><td>{esc(t['status'])}</td>"
                    f"<td>{t.get('round', 0)}</td><td>{esc(t.get('feedback', ''))}</td><td>{evidence}</td></tr>")
    text = """<!doctype html><html lang="en"><meta charset="utf-8"><meta http-equiv="refresh" content="3">
<title>Devin Gauntlet</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px}
table{border-collapse:collapse;width:100%}td,th{padding:12px;text-align:left;border-bottom:1px solid #bbb}
pre{white-space:pre-wrap}td:nth-child(4){max-width:500px}</style>"""
    text += f"<h1>{esc(state.get('progress_title', 'Devin Gauntlet'))}</h1><p><b>{esc(state['status'])}</b> · wave {state.get('wave', 0)}</p>"
    text += "<table><tr><th>Workstream</th><th>Status</th><th>Build round</th><th>Feedback</th><th>Evidence</th></tr>" + "".join(rows) + "</table>"
    text += f"<pre>{esc(state.get('error', ''))}</pre><p>Operational state is maintained by Python, not by agent prose.</p></html>"
    atomic(root / "public/index.html", text, text=True)
