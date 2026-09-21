#!/usr/bin/env python3
"""Run a real subprocess/scheduler demonstration with an explicitly fake agent."""
from pathlib import Path
import argparse
import json
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
from devin_gauntlet.common import atomic, git, load
from devin_gauntlet.engine import initialize
from devin_gauntlet.__main__ import main

p = argparse.ArgumentParser()
p.add_argument("directory", type=Path)
args = p.parse_args()
base = args.directory.resolve()
if base.exists() and any(base.iterdir()):
    raise SystemExit("Choose an empty demo directory")
base.mkdir(parents=True, exist_ok=True)
repo = base / "project"
repo.mkdir()
git(repo, "init", "--quiet")
(repo / "score.txt").write_text("0\n")
git(repo, "add", ".")
git(repo, "commit", "--quiet", "-m", "Demo input")
ref = base / "reference.txt"
ref.write_text("6\n")
run = base / "run"
initialize(run, repo, "Make the numeric demo artifact exceed the reference", [ref])
fixture = base / "fixture.json"
atomic(fixture, {"plan": {"progress_title": "OFFLINE FAKE-AGENT DEMO", "tasks": [{
    "id": "score", "goal": "Exceed reference value", "bar": "The larger actual integer wins",
    "mode": "ab", "depends_on": [], "rules": [], "reference_files": ["input:0"],
    "prepare": [], "capture": [], "checks": [], "artifacts": ["score.txt"],
}]}})
config = load(run / "config.json")
config["agent_command"] = [sys.executable, str(PACKAGE / "examples/fake_devin.py"), "--fixture", str(fixture)]
atomic(run / "config.json", config)
code = main(["run", str(run), "--allow-execution"])
state = load(run / "state.json")
print("\nThis was NOT a Devin model run.")
print("Build rounds:", state["tasks"]["score"]["round"])
print("Original file:", (repo / "score.txt").read_text().strip())
print("Integrated file:", (run / "work/integration/score.txt").read_text().strip())
print("Patch:", run / "result.patch")
raise SystemExit(code)
