from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .common import Blocked, Stopped, atomic, load
from .engine import Engine, initialize


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Durable external Gauntlet scheduler for Devin CLI")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create an isolated run without changing the original repository")
    init.add_argument("run_dir", type=Path)
    init.add_argument("--project", type=Path, required=True)
    init.add_argument("--goal", required=True)
    init.add_argument("--reference", type=Path, action="append", default=[])
    init.add_argument("--parallel", type=int, default=2)
    init.add_argument("--model", default=None, help="Optional Devin model; otherwise use the CLI default")
    for name in ("plan", "run", "resume"):
        cmd = sub.add_parser(name)
        cmd.add_argument("run_dir", type=Path)
        cmd.add_argument("--allow-execution", action="store_true",
                         help="Authorize agent edits and lead-generated local capture/test commands. "
                              "These commands can execute project code and access the network. "
                              "Trust the project and use a disposable environment.")
        cmd.add_argument("--max-seconds", type=float, default=None,
                         help="Optional user-chosen wall-time cap for this invocation, not a quality-round limit")
        if name != "plan":
            cmd.add_argument("--smoothing", action="store_true", help="Enable optional write-enabled smoothing before final rechecks")
    for name in ("status", "stop", "export"):
        cmd = sub.add_parser(name)
        cmd.add_argument("run_dir", type=Path)
    sub.add_parser("doctor", help="Inspect local prerequisites without starting a paid model session")
    return p


def doctor() -> int:
    details = {"python": sys.version.split()[0], "platform": sys.platform,
               "git": shutil.which("git"), "devin": shutil.which("devin")}
    if sys.platform.startswith("linux"):
        details.update(bwrap=shutil.which("bwrap"), socat=shutil.which("socat"))
    if details["devin"]:
        proc = subprocess.run([details["devin"], "--help"], capture_output=True, text=True, timeout=20)
        help_text = proc.stdout + proc.stderr
        details["required_flags"] = {flag: flag in help_text for flag in
                                      ("--print", "--prompt-file", "--config", "--respect-workspace-trust", "--sandbox")}
        proc = subprocess.run([details["devin"], "--version"], capture_output=True, text=True, timeout=20)
        details["devin_version"] = (proc.stdout + proc.stderr).strip()
    print(json.dumps(details, ensure_ascii=False, indent=2))
    ready = bool(details["git"] and details["devin"] and os.name == "posix")
    ready = ready and all(details.get("required_flags", {}).values())
    if sys.platform.startswith("linux"):
        ready = ready and bool(details["bwrap"] and details["socat"])
    return 0 if ready else 2


async def execute(args: argparse.Namespace) -> int:
    engine = Engine(args.run_dir, max_seconds=args.max_seconds)
    with engine.lock():
        # A STOP flag is cleared only by an explicit user run/resume command.
        (engine.root / "STOP").unlink(missing_ok=True)
        start = time.monotonic()
        try:
            if args.command == "plan":
                if (engine.root / "plan.json").exists():
                    raise Blocked("This run already has a frozen plan; use a new run to change the bar")
                await engine.prepare_plan()
            else:
                if not (engine.root / "plan.json").exists():
                    await engine.prepare_plan()
                await engine.run(smoothing=args.smoothing)
            returncode = 0
        except Stopped as exc:
            engine.state.update(status=str(exc), error="")
            returncode = 130 if str(exc) == "USER_STOPPED" else 3
        except Exception as exc:
            engine.state.update(status="BLOCKED", error=f"{type(exc).__name__}: {exc}")
            engine.event("blocked", detail=engine.state["error"])
            returncode = 2
        finally:
            engine.state["active_seconds"] = engine.state.get("active_seconds", 0) + time.monotonic() - start
            engine.save()
        print(json.dumps({"status": engine.state["status"], "run_dir": str(engine.root),
                          "error": engine.state.get("error", ""),
                          "progress": str(engine.root / "public/index.html")}, ensure_ascii=False, indent=2))
        return returncode


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor()
        if os.name != "posix":
            raise Blocked("Use Linux/WSL2 or macOS. Native Windows process supervision is not implemented.")
        if args.command == "init":
            initialize(args.run_dir, args.project, args.goal, args.reference,
                       parallel=args.parallel, model=args.model)
            print(str(args.run_dir.resolve() / "config.json"))
            return 0
        if args.command == "status":
            print(json.dumps(load(args.run_dir / "state.json"), ensure_ascii=False, indent=2))
            return 0
        if args.command == "stop":
            atomic(args.run_dir.resolve() / "STOP", "User requested stop\n", text=True)
            print("Stop requested. Check status for USER_STOPPED after the supervisor terminates its managed process groups.")
            return 0
        if args.command == "export":
            engine = Engine(args.run_dir)
            with engine.lock():
                print(engine.export_patch())
            return 0
        if not args.allow_execution:
            raise Blocked("Pass --allow-execution to authorize local agent execution and generated capture/test commands. "
                          "Review README.md's execution boundaries first.")
        if args.max_seconds is not None and args.max_seconds <= 0:
            raise Blocked("--max-seconds must be positive")
        return asyncio.run(execute(args))
    except (Blocked, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
