from __future__ import annotations

import asyncio
import json
import os
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from .common import Blocked, Stopped, atomic, parse_result


class Runtime:
    """Owns process lifetimes. A model response cannot stop the scheduler."""

    def __init__(self, root: Path, config: dict, event, *, max_seconds: float | None = None):
        self.root, self.config, self.event = root, config, event
        self.semaphore = asyncio.Semaphore(config.get("max_parallel", 2))
        self.stop_event = asyncio.Event()
        self.started = time.monotonic()
        self.max_seconds = max_seconds
        self.stop_reason = "USER_STOPPED"
        self.active: dict[str, dict] = {}

    def check_stop(self) -> None:
        if self.max_seconds is not None and time.monotonic() - self.started >= self.max_seconds:
            self.stop_reason = "USER_LIMIT_REACHED"
            self.stop_event.set()
        if self.stop_event.is_set() or (self.root / "STOP").exists():
            raise Stopped(self.stop_reason)

    def save_active(self) -> None:
        atomic(self.root / "private/active.json", self.active)

    async def terminate_group(self, proc: asyncio.subprocess.Process) -> None:
        # start_new_session makes the child's PID its process-group ID. This also
        # cleans ordinary descendants after a successful parent exit.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()

    async def process(self, argv: list[str], cwd: Path, role: str,
                      *, env_extra: dict[str, str] | None = None) -> dict:
        self.check_stop()
        async with self.semaphore:
            self.check_stop()
            ident = uuid.uuid4().hex
            logs = self.root / "private/calls" / ident
            logs.mkdir(parents=True)
            started = time.monotonic()
            metadata = {"argv": argv, "cwd": str(cwd), "role": role, "id": ident}
            atomic(logs / "request.json", metadata)
            env = os.environ.copy()
            env.update(env_extra or {})
            env["NO_COLOR"] = "1"
            try:
                with (logs / "stdout.txt").open("wb") as out, (logs / "stderr.txt").open("wb") as err:
                    proc = await asyncio.create_subprocess_exec(
                        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                        stdout=out, stderr=err, start_new_session=True,
                    )
                    self.active[ident] = {**metadata, "pid": proc.pid}
                    self.save_active()
                    self.event("process_started", role=role, call=ident, pid=proc.pid)
                    waiter = asyncio.create_task(proc.wait())
                    try:
                        while proc.returncode is None:
                            self.check_stop()
                            timeout = self.config.get("call_timeout_seconds")
                            if timeout is not None and time.monotonic() - started >= timeout:
                                raise Blocked(f"{role} call timed out; not a quality pass")
                            try:
                                await asyncio.wait_for(asyncio.shield(waiter), timeout=0.2)
                            except asyncio.TimeoutError:
                                pass
                    finally:
                        await self.terminate_group(proc)
                        await waiter
                        self.active.pop(ident, None)
                        self.save_active()
            except FileNotFoundError as exc:
                raise Blocked(f"Executable not found: {argv[0]}") from exc
            output = (logs / "stdout.txt").read_text(encoding="utf-8", errors="replace")
            error = (logs / "stderr.txt").read_text(encoding="utf-8", errors="replace")
            result = {"returncode": proc.returncode, "stdout": output, "stderr": error, "call": ident}
            atomic(logs / "result.json", {"returncode": proc.returncode, "seconds": time.monotonic() - started})
            self.event("process_finished", role=role, call=ident, returncode=proc.returncode)
            return result

    def permissions(self, cwd: Path, role: str) -> dict:
        readonly = role == "critic"
        allow = [f"Read({cwd}/**)"]
        deny = ["mcp__*", f"Write({self.config['project_dir']}/**)"]
        # The run logs and A/B map are not inputs to any model role.
        for p in (self.root / "private", self.root / "public"):
            deny += [f"Read({p}/**)", f"Write({p}/**)"]
        if readonly:
            deny += ["exec", "edit", "Write(/**)",
                     f"Read({self.root / 'work'}/**)",
                     f"Read({self.root / 'references'}/**)",
                     f"Read({self.config['project_dir']}/**)",
                     f"Read({Path.home() / '.config/devin'}/**)",
                     f"Read({Path.home() / '.local/share/devin'}/**)"]
        else:
            allow += [f"Write({cwd}/**)", "Exec(**)"]
            deny += [f"Write({cwd / '.git'}/**)", f"Write({cwd / '.gauntlet-runtime'}/**)",
                     f"Write({self.root / 'references'}/**)"]
        return {
            "permissions": {"allow": allow, "deny": deny},
            "read_config_from": {"cursor": False, "windsurf": False, "claude": False},
            "sandbox": {"excluded": {"deny": ["Exec(**)"]}},
        }

    async def agent(self, role: str, cwd: Path, instruction: str, payload: dict,
                    *, structured: bool = True) -> dict | str:
        nonce = uuid.uuid4().hex
        inputs = cwd / ".gauntlet-runtime" / nonce
        inputs.mkdir(parents=True)
        config = self.permissions(cwd, role)
        atomic(inputs / "config.json", config)
        # This file IS the CLI's initial prompt, not an instruction to read another
        # spec document. The scheduler itself is ordinary Python control flow.
        request = {"role": role, **payload}
        body = instruction + "\n\nGAUNTLET_REQUEST_JSON\n" + json.dumps(request, ensure_ascii=False, indent=2)
        body += "\nEND_GAUNTLET_REQUEST_JSON\n"
        if structured:
            body += f"\nReturn one result as <GAUNTLET_RESULT:{nonce}>JSON_OBJECT</GAUNTLET_RESULT>.\n"
        else:
            body += "\nYour completion message is a report, never a quality verdict. Do not start background or cloud agents.\n"
        if role != "critic":
            body += ("\nENVIRONMENT NOTE: the `edit`/`write` file tools are unavailable in this "
                     "non-interactive sandboxed mode — calls to them are rejected. Create and modify "
                     "ALL files via the exec/shell tool (heredocs, python -c, cp, tee, etc.).\n")
        atomic(inputs / "prompt.md", body, text=True)
        argv = list(self.config.get("agent_command", ["devin"]))
        argv += ["--print", "--prompt-file", str(inputs / "prompt.md"),
                 "--config", str(inputs / "config.json"),
                 "--respect-workspace-trust", "false"]
        # Normal mode for the read-only critic: shell tools are explicitly denied.
        # Writable roles use the documented OS sandbox, not dangerous/bypass mode.
        if role != "critic":
            argv += ["--sandbox", "--permission-mode", "autonomous"]
        else:
            argv += ["--permission-mode", "normal"]
        model = self.config.get("models", {}).get(role)
        if model:
            argv += ["--model", model]
        # A single transient failure (nonzero exit, malformed envelope, upstream
        # 5xx) must not kill the whole run — retry before declaring Blocked.
        last_error = "unknown failure"
        for attempt in range(3):
            result = await self.process(argv, cwd, role)
            if result["returncode"] == 0:
                if not structured:
                    return result["stdout"]
                try:
                    return parse_result(result["stdout"], nonce)
                except Blocked as exc:
                    last_error = f"unusable structured output: {exc}"
            else:
                last_error = (f"{role} exited {result['returncode']}: "
                              f"{result['stderr'][-2000:]}; call={result['call']}")
            if attempt < 2:
                await asyncio.sleep(5)
        raise Blocked(f"{role} failed after 3 attempts. Last: {last_error}")
