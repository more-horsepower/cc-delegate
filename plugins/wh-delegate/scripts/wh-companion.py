#!/usr/bin/env python3
"""Workhorse delegate companion.

A single-file, zero-dependency Python companion that drives opencode on
Workhorse inference from Claude Code. Run via:

    uv run "${CLAUDE_PLUGIN_ROOT}/scripts/wh-companion.py" <command> [...]

Subcommands:
  setup                      verify wh / opencode / proxy / providers
  task                       run (or enqueue) an opencode task
  task-worker                detached worker for a queued background task
  task-resume-candidate      report the latest resumable task for this session
  transfer                   import the current Claude transcript into opencode
  status                     list / inspect tracked jobs
  result                     show stored output for a finished job
  cancel                     cancel an active background job
  broker                     inspect (or stop) the per-workspace opencode broker
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --- constants ---------------------------------------------------------------

SESSION_ID_ENV = "WH_DELEGATE_SESSION_ID"
TRANSCRIPT_PATH_ENV = "WH_DELEGATE_TRANSCRIPT_PATH"
PLUGIN_DATA_ENV = "CLAUDE_PLUGIN_DATA"
DEFAULT_MODEL = "workhorse-proxy/default"
DEFAULT_CONTINUE_PROMPT = "Continue the previous task from where you left off."
MAX_JOBS = 50
DEFAULT_WAIT_TIMEOUT_MS = 240_000
DEFAULT_POLL_MS = 2000
OPENCODE_WH_DIRS = [
    Path.home() / ".opencode-wh" / "bin",
    Path.home() / ".opencode-wh",
    Path.home() / ".opencode" / "bin",
]
PROJECTS_DIR = (Path.home() / ".claude" / "projects").resolve()
BROKER_HOSTNAME = "127.0.0.1"
BROKER_START_TIMEOUT_S = 20
ABORTED_ERROR = "MessageAbortedError"

# --- tiny helpers ------------------------------------------------------------


def err(msg: str) -> None:
    print(msg, file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    err(msg)
    sys.exit(code)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_version(s: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", s or "")
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def run(cmd: list[str], *, timeout: int | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"command not found: {cmd[0]}")
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 124, e.stdout or "", e.stderr or "timed out")


def shorten(text: str, limit: int = 96) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


def first_line(text: str, fallback: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return fallback


def emit(value, as_json: bool) -> None:
    """Print a rendered string or a JSON payload depending on --json."""
    if as_json:
        print(json.dumps(value, indent=2))
    else:
        sys.stdout.write(value if isinstance(value, str) else str(value))


# --- opencode / wh discovery -------------------------------------------------


def find_opencode_binary() -> str | None:
    candidates: list[tuple[tuple[int, ...], str]] = []
    seen: set[str] = set()

    def consider(path: str | Path | None) -> None:
        if not path:
            return
        path = str(path)
        if path in seen:
            return
        seen.add(path)
        v = run([path, "--version"], timeout=10)
        if v.returncode == 0 and v.stdout.strip():
            candidates.append((parse_version(v.stdout.strip()), path))

    consider(shutil.which("opencode"))
    for d in OPENCODE_WH_DIRS:
        consider(d / "opencode")
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def find_wh_binary() -> str | None:
    return shutil.which("wh")


# --- state --------------------------------------------------------------------


def workspace_root(cwd: str) -> str:
    r = run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], timeout=5)
    return r.stdout.strip() if r.returncode == 0 else cwd


def state_dir(cwd: str) -> Path:
    root = workspace_root(cwd)
    try:
        root = str(Path(root).resolve())
    except Exception:
        pass
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(root).name) or "workspace"
    digest = hashlib.sha256(root.encode()).hexdigest()[:16]
    base = os.environ.get(PLUGIN_DATA_ENV)
    root_dir = Path(base) / "state" if base else Path(os.environ.get("TMPDIR", "/tmp")) / "wh-companion"
    return root_dir / f"{slug}-{digest}"


def _load_index(cwd: str) -> list[dict]:
    f = state_dir(cwd) / "state.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text()).get("jobs", [])
    except Exception:
        return []


def _save_index(cwd: str, jobs: list[dict]) -> None:
    d = state_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    jobs = sorted(jobs, key=lambda j: j.get("updatedAt", ""), reverse=True)[:MAX_JOBS]
    (d / "state.json").write_text(json.dumps({"version": 1, "jobs": jobs}, indent=2) + "\n")


def upsert_job(cwd: str, patch: dict) -> None:
    """Update (or insert) a job in the index. Always carries identity fields."""
    # Ensure identity/scope fields are preserved on every upsert so the index
    # alone is sufficient for status/result/resume filtering.
    full = {k: v for k, v in patch.items() if k in {
        "id", "kind", "kindLabel", "title", "summary", "workspaceRoot",
        "jobClass", "sessionId", "status", "phase", "pid", "opencodePid",
        "threadId", "threadDir", "startedAt", "completedAt", "logFile", "errorMessage",
    }}
    jobs = _load_index(cwd)
    ts = now_iso()
    for i, j in enumerate(jobs):
        if j["id"] == patch["id"]:
            jobs[i] = {**j, **full, "updatedAt": ts}
            _save_index(cwd, jobs)
            return
    jobs.insert(0, {"createdAt": ts, "updatedAt": ts, **full})
    _save_index(cwd, jobs)


def list_jobs(cwd: str) -> list[dict]:
    return _load_index(cwd)


def jobs_dir(cwd: str) -> Path:
    d = state_dir(cwd) / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_file(cwd: str, jid: str) -> Path:
    return jobs_dir(cwd) / f"{jid}.json"


def log_path(cwd: str, jid: str) -> Path:
    return jobs_dir(cwd) / f"{jid}.log"


def read_job(cwd: str, jid: str) -> dict | None:
    f = job_file(cwd, jid)
    return json.loads(f.read_text()) if f.exists() else None


def write_job(cwd: str, jid: str, data: dict) -> None:
    job_file(cwd, jid).write_text(json.dumps(data, indent=2) + "\n")


def append_log(cwd: str, jid: str, msg: str) -> None:
    if not msg or not msg.strip():
        return
    with log_path(cwd, jid).open("a") as fh:
        fh.write(f"[{now_iso()}] {msg.strip()}\n")


def new_job_id(prefix: str = "task") -> str:
    import random

    return f"{prefix}-{now_ms():x}-{random.randrange(0, 16**6):06x}"


def session_id() -> str | None:
    return os.environ.get(SESSION_ID_ENV)


def _mark_cancelled(cwd: str, workspace: str, jid: str, message: str) -> None:
    """Record a job as cancelled. Keeps threadId/threadDir so the opencode
    session stays resumable after a clean broker abort."""
    done = now_iso()
    stored = read_job(workspace, jid) or {}
    stored.update(status="cancelled", phase="cancelled", pid=None, opencodePid=None,
                  completedAt=done, errorMessage=message)
    write_job(workspace, jid, stored)
    upsert_job(cwd, {"id": jid, "status": "cancelled", "phase": "cancelled",
                     "pid": None, "opencodePid": None, "completedAt": done})


def reap_session_jobs(cwd: str, sid: str) -> int:
    """Cancel active jobs for Claude session `sid` and mark them cancelled.

    Each running turn is stopped through the broker's abort API so opencode
    flushes its state and the worker observes a clean cancellation (recorded
    as cancelled, not failed). Jobs stay in the index for history/resume.
    Falls back to a process-group SIGTERM only when the broker is unreachable."""
    if not cwd or not sid:
        return 0
    jobs = list_jobs(cwd)
    active = [j for j in jobs if j.get("sessionId") == sid and j["status"] in ("queued", "running")]
    if not active:
        return 0
    broker = read_broker(cwd)
    healthy = bool(broker and _pid_alive(broker.get("pid")) and broker_healthy(broker["url"], timeout=1))
    for j in active:
        tid = j.get("threadId")
        via = None
        if healthy and tid and broker_abort_session(broker, j.get("threadDir") or cwd, tid, timeout=1.5):
            via = "broker abort"
        else:
            kill_pid = j.get("opencodePid") or j.get("pid")
            if kill_pid:
                try:
                    os.killpg(kill_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except Exception:
                    try:
                        os.kill(kill_pid, signal.SIGTERM)
                    except Exception:
                        pass
            via = "process stop"
        append_log(cwd, j["id"], f"Claude session ended; job cancelled ({via}).")
        _mark_cancelled(cwd, j.get("workspaceRoot") or cwd, j["id"], "Cancelled: Claude session ended.")
    return len(active)


def current_session_jobs(jobs: list[dict]) -> list[dict]:
    sid = session_id()
    return [j for j in jobs if not sid or j.get("sessionId") == sid]


# --- opencode serve broker ---------------------------------------------------
#
# One persistent `opencode serve` broker per workspace. Tasks run as
# `opencode run --attach <broker-url>` against pre-created sessions, and cancel
# goes through POST /session/{id}/abort so opencode stops the turn itself,
# flushes its SQLite state, and leaves the session idle + resumable.
# The broker's lifecycle is tied to Claude sessions via lease files: the
# SessionStart hook takes a lease, SessionEnd drops it, and the broker is
# stopped when the last lease goes away.


def _http_json(method: str, url: str, payload=None, timeout: float = 5):
    """JSON HTTP call. Returns (ok, payload).

    Guards against the opencode SPA fallback, which answers unknown API paths
    with 200 text/html — a JSON content-type check is the only reliable signal."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if "application/json" not in (resp.headers.get("Content-Type") or ""):
                return False, None
            raw = resp.read().decode()
            return True, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode()
            return False, (json.loads(raw) if raw.strip() else None)
        except Exception:
            return False, None
    except Exception:
        return False, None


def _broker_paths(cwd: str) -> tuple[Path, Path, Path]:
    d = state_dir(cwd)
    return d / "broker.json", d / "broker.log", d / "broker.lock"


def read_broker(cwd: str) -> dict | None:
    f, _, _ = _broker_paths(cwd)
    try:
        b = json.loads(f.read_text())
        return b if b.get("url") and b.get("pid") else None
    except Exception:
        return None


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def broker_healthy(url: str | None, timeout: float = 2) -> dict | None:
    if not url:
        return None
    ok, payload = _http_json("GET", f"{url}/global/health", timeout=timeout)
    return payload if ok and isinstance(payload, dict) and payload.get("healthy") else None


def _parse_listen_url(log_file: Path) -> str | None:
    try:
        matches = re.findall(r"opencode server listening on (http://\S+)", log_file.read_text())
    except Exception:
        return None
    return matches[-1] if matches else None


def ensure_broker(cwd: str) -> dict | None:
    """Return {'url', 'pid', ...} for the workspace broker, starting it if needed.

    Serialized with a flock so concurrent task/worker spawns share one broker."""
    bj, blog, block = _broker_paths(cwd)
    state_dir(cwd).mkdir(parents=True, exist_ok=True)
    with open(block, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = read_broker(cwd)
        if existing and _pid_alive(existing.get("pid")):
            health = broker_healthy(existing["url"])
            if health:
                return {**existing, "version": health.get("version")}
            # Alive but not answering: stop it before replacing.
            try:
                os.killpg(int(existing["pid"]), signal.SIGTERM)
            except Exception:
                pass
            time.sleep(0.3)
        binary = find_opencode_binary()
        if not binary:
            return None
        workspace = workspace_root(cwd)
        log_fh = open(blog, "wb")
        try:
            proc = subprocess.Popen(
                [binary, "serve", "--port", "0", "--hostname", BROKER_HOSTNAME],
                cwd=workspace, stdin=subprocess.DEVNULL, stdout=log_fh,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
        except FileNotFoundError:
            log_fh.close()
            return None
        log_fh.close()  # the child holds its own dup of the fd
        url = None
        deadline = time.time() + BROKER_START_TIMEOUT_S
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            candidate = _parse_listen_url(blog)
            if candidate and broker_healthy(candidate, timeout=1):
                url = candidate
                break
            time.sleep(0.25)
        if not url:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                pass
            return None
        broker = {"url": url, "pid": proc.pid, "workspaceRoot": workspace, "startedAt": now_iso()}
        bj.write_text(json.dumps(broker, indent=2) + "\n")
        return broker


def stop_broker(cwd: str) -> bool:
    """SIGTERM the workspace broker (its own process group) and forget it."""
    broker = read_broker(cwd)
    bj, _, _ = _broker_paths(cwd)
    stopped = False
    if broker and _pid_alive(broker.get("pid")):
        try:
            os.killpg(int(broker["pid"]), signal.SIGTERM)
            stopped = True
        except ProcessLookupError:
            pass
        except Exception:
            try:
                os.kill(int(broker["pid"]), signal.SIGTERM)
                stopped = True
            except Exception:
                pass
    try:
        bj.unlink()
    except Exception:
        pass
    return stopped


def broker_create_session(broker: dict, directory: str, title: str | None = None) -> str | None:
    q = urllib.parse.quote(str(directory), safe="")
    ok, payload = _http_json("POST", f"{broker['url']}/session?directory={q}",
                             {"title": title} if title else {}, timeout=10)
    return payload.get("id") if ok and isinstance(payload, dict) else None


def broker_abort_session(broker: dict | None, directory: str, sid: str | None, timeout: float = 3) -> bool:
    if not broker or not sid:
        return False
    q = urllib.parse.quote(str(directory), safe="")
    ok, payload = _http_json("POST", f"{broker['url']}/session/{sid}/abort?directory={q}", timeout=timeout)
    return bool(ok and payload is True)


def _leases_dir(cwd: str) -> Path:
    d = state_dir(cwd) / "leases"
    d.mkdir(parents=True, exist_ok=True)
    return d


def lease_start(cwd: str, sid: str | None) -> None:
    if not sid:
        return
    try:
        (_leases_dir(cwd) / f"{sid}.json").write_text(
            json.dumps({"sessionId": sid, "createdAt": now_iso()}) + "\n")
    except Exception:
        pass


def lease_end(cwd: str, sid: str | None) -> int:
    """Drop this Claude session's lease; return the number of leases remaining."""
    if sid:
        try:
            (_leases_dir(cwd) / f"{sid}.json").unlink()
        except Exception:
            pass
    return lease_count(cwd)


def lease_count(cwd: str) -> int:
    try:
        return len(list(_leases_dir(cwd).glob("*.json")))
    except Exception:
        return 0


# --- opencode task runner ----------------------------------------------------


def _describe_tool(part: dict) -> str:
    name = part.get("tool", "tool")
    state = part.get("state", {}) or {}
    title = state.get("title", "")
    suffix = f" — {title}" if title else ""
    if state.get("status") == "completed":
        return f"Tool completed: {name}{suffix}"
    if state.get("status") == "error":
        return f"Tool failed: {name}"
    return f"Running tool: {name}{suffix}"


def run_opencode_turn(cwd, prompt, model, variant, session_id, is_resume, broker_url, on_text, on_progress, on_start=None):
    """Run one opencode turn through the workspace broker.

    Spawns `opencode run --attach <broker_url> --session <session_id>` so the
    turn is owned by the persistent broker: cancellation goes through the
    broker's abort API (clean turn stop, flushed state, idle + resumable
    session) instead of killing the process. Returns a dict:
      status (int), aborted (bool), sessionId, raw (str), error (str)

    `on_start(proc_pid)` is called right after the opencode CLI is spawned so
    callers can record the process-group leader as a last-resort stop path."""
    binary = find_opencode_binary()
    if not binary:
        return {"status": 1, "aborted": False, "sessionId": session_id, "raw": "",
                "error": "opencode binary not found. Run /wh:setup to diagnose."}

    effective = prompt or (DEFAULT_CONTINUE_PROMPT if is_resume else "")
    if not effective:
        return {"status": 1, "aborted": False, "sessionId": session_id, "raw": "",
                "error": "Provide a prompt or use --resume."}

    args = [binary, "run"]
    if effective:
        args.append(effective)
    args += ["--dir", cwd, "--format", "json", "--auto"]
    if model:
        args += ["--model", model]
    if variant:
        args += ["--variant", variant]
    if broker_url:
        args += ["--attach", broker_url]
    if session_id:
        args += ["--session", session_id]

    try:
        proc = subprocess.Popen(
            args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
    except FileNotFoundError:
        return {"status": 1, "aborted": False, "sessionId": session_id, "raw": "",
                "error": "opencode binary not found."}

    if on_start:
        on_start(proc.pid)

    sid = session_id
    texts: list[str] = []
    failure = ""
    aborted = False

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if not sid and ev.get("sessionID"):
            sid = ev["sessionID"]
        t = ev.get("type")
        if t == "text":
            txt = (ev.get("part") or {}).get("text", "")
            if txt:
                texts.append(txt)
                on_text(txt)
        elif t == "tool_use":
            on_progress(_describe_tool(ev.get("part") or {}), "investigating")
        elif t == "step_start":
            on_progress("Step started", "starting")
        elif t == "step_finish":
            on_progress("Step finished", "finalizing")
        elif t == "error":
            e = ev.get("error") or {}
            msg = (e.get("data") or {}).get("message") or e.get("name") or "opencode error"
            if e.get("name") == ABORTED_ERROR:
                # The turn was aborted through the broker (cancel / session end):
                # a clean stop, not a failure.
                aborted = True
                on_progress("Turn aborted.", "cancelled")
            else:
                failure = f"{failure}\n{msg}" if failure else msg
                on_progress(f"opencode error: {msg}", "failed")

    proc.wait()
    stderr = proc.stderr.read().strip()
    raw = "".join(texts)
    status = 1 if failure else proc.returncode
    error = failure or (stderr if status else "")
    return {"status": status, "aborted": aborted, "sessionId": sid, "raw": raw, "error": error}


# --- task metadata / resume --------------------------------------------------


def task_metadata(prompt: str, resume: bool) -> dict:
    title = "opencode Resume" if resume else "opencode Task"
    summary = shorten(prompt or (DEFAULT_CONTINUE_PROMPT if resume else "Task"))
    return {"title": title, "summary": summary}


def resolve_resume_candidate(workspace: str, exclude_job: str | None = None) -> dict | None:
    jobs = sorted(current_session_jobs(list_jobs(workspace)), key=lambda j: j.get("updatedAt", ""), reverse=True)
    if exclude_job:
        jobs = [j for j in jobs if j["id"] != exclude_job]
    active = next((j for j in jobs if j.get("jobClass") == "task" and j["status"] in ("queued", "running")), None)
    if active:
        die(f"Task {active['id']} is still running. Use /wh:status before continuing it.")
    return next(
        (j for j in jobs if j.get("jobClass") == "task" and j.get("threadId") and j["status"] not in ("queued", "running")),
        None,
    )


# --- task command ------------------------------------------------------------


def _make_job(workspace: str, meta: dict) -> dict:
    job = {
        "id": new_job_id("task"),
        "kind": "task",
        "kindLabel": "task",
        "title": meta["title"],
        "summary": meta["summary"],
        "workspaceRoot": workspace,
        "jobClass": "task",
    }
    sid = session_id()
    if sid:
        job["sessionId"] = sid
    return job


def _track_running(cwd: str, workspace: str, job: dict) -> None:
    job.update(status="running", phase="starting", pid=os.getpid(), startedAt=now_iso(), logFile=str(log_path(cwd, job["id"])))
    write_job(workspace, job["id"], job)
    upsert_job(cwd, job)


def _finalize(cwd: str, workspace: str, job: dict, result: dict) -> dict:
    """Record the finished turn. Returns the stored result payload.

    If /wh:cancel (or SessionEnd) already marked the job cancelled, that
    verdict wins — the worker observed the same clean abort and must not
    overwrite it with 'failed'."""
    jid = job["id"]
    stored = read_job(workspace, jid) or {}
    if stored.get("status") == "cancelled":
        if not stored.get("threadId") and result.get("sessionId"):
            stored["threadId"] = result["sessionId"]
            write_job(workspace, jid, stored)
            upsert_job(cwd, {"id": jid, "threadId": result["sessionId"]})
        job.update({k: v for k, v in stored.items() if k != "request"})
        return stored.get("result") or {"status": result.get("status"), "threadId": stored.get("threadId"),
                                        "rawOutput": result.get("raw", ""), "error": result.get("error", "")}
    if result.get("aborted"):
        done = "cancelled"
    else:
        done = "completed" if result.get("status") == 0 else "failed"
    sid = result.get("sessionId") or job.get("threadId")
    payload = {"status": result.get("status"), "threadId": sid,
               "rawOutput": result.get("raw", ""), "error": result.get("error", "")}
    job.update(status=done, phase=done, threadId=sid, pid=None, opencodePid=None, completedAt=now_iso(),
               result=payload)
    write_job(workspace, jid, job)
    upsert_job(cwd, job)
    return payload


def _progress_for(cwd: str, jid: str, workspace: str):
    def _cb(msg: str, phase: str | None = None) -> None:
        append_log(cwd, jid, msg)
        if phase:
            upsert_job(cwd, {"id": jid, "phase": phase})
    return _cb


def cmd_task(argv) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="wh-companion task", add_help=False)
    p.add_argument("prompt", nargs="*")
    p.add_argument("--model")
    p.add_argument("--variant")
    p.add_argument("--cwd")
    p.add_argument("--prompt-file")
    p.add_argument("--background", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--resume-last", action="store_true", dest="resume")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if args.resume and args.fresh:
        die("Choose either --resume/--resume-last or --fresh.")

    cwd = args.cwd or os.getcwd()
    workspace = workspace_root(cwd)
    model = args.model or os.environ.get("WH_DELEGATE_DEFAULT_MODEL", DEFAULT_MODEL)

    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text()
    else:
        prompt = " ".join(args.prompt)
        if not prompt and not sys.stdin.isatty():
            prompt = sys.stdin.read()

    meta = task_metadata(prompt, args.resume)
    job = _make_job(workspace, meta)

    if args.background:
        if not prompt and not args.resume:
            die("Provide a prompt, a prompt file, piped stdin, or use --resume/--resume-last.")
        if not find_opencode_binary():
            die("opencode is not installed. Run /wh:setup to diagnose.")
        _enqueue_background(cwd, workspace, job, {"cwd": cwd, "model": model, "variant": args.variant, "prompt": prompt, "resume": args.resume})
        if args.json:
            print(json.dumps({"jobId": job["id"], "status": "queued", "title": job["title"], "summary": job["summary"]}, indent=2))
        else:
            emit(f"{job['title']} started in the background as {job['id']}. Check /wh:status {job['id']} for progress.\n", False)
        return

    cand = resolve_resume_candidate(workspace) if args.resume else None
    resume_id = cand["threadId"] if cand else None
    if not prompt and not resume_id:
        die("Provide a prompt, a prompt file, piped stdin, or use --resume/--resume-last.")

    broker = ensure_broker(cwd)
    if not broker:
        die("Could not start the opencode broker (opencode serve). Run /wh:setup to diagnose.")
    # Create the opencode session up front (unless resuming one) so the job
    # carries its threadId from the start and cancel can abort immediately.
    thread_id = resume_id or broker_create_session(broker, cwd, title=meta["summary"])
    if not thread_id:
        die("The opencode broker did not create a session. Run /wh:setup to diagnose.")
    thread_dir = (cand or {}).get("threadDir") or cwd
    job["threadId"] = thread_id
    job["threadDir"] = thread_dir

    _track_running(cwd, workspace, job)
    progress = _progress_for(cwd, job["id"], workspace)

    def on_text(txt: str) -> None:
        if not args.json:
            sys.stdout.write(txt + "\n")
            sys.stdout.flush()
        append_log(cwd, job["id"], f"Assistant: {txt}")

    def on_start(proc_pid: int) -> None:
        upsert_job(cwd, {"id": job["id"], "opencodePid": proc_pid})

    def _interrupted(signum, _frame) -> None:
        # Claude Code kills the foreground companion on interrupt; stop the
        # opencode turn through the broker instead of leaving it running.
        broker_abort_session(broker, thread_dir, thread_id, timeout=2)
        _mark_cancelled(cwd, workspace, job["id"], "Cancelled: interrupted.")
        sys.exit(128 + int(signum))

    signal.signal(signal.SIGTERM, _interrupted)
    signal.signal(signal.SIGINT, _interrupted)

    try:
        result = run_opencode_turn(cwd, prompt, model, args.variant, thread_id, bool(resume_id),
                                   broker["url"], on_text, progress, on_start)
    except Exception as e:
        broker_abort_session(broker, thread_dir, thread_id, timeout=2)
        _finalize(cwd, workspace, job, {"status": 1, "aborted": False, "sessionId": thread_id, "raw": "", "error": str(e)})
        raise
    payload = _finalize(cwd, workspace, job, result)
    if args.json:
        print(json.dumps(payload, indent=2))
    elif not result["raw"]:
        note = "Cancelled." if job.get("status") == "cancelled" else (result["error"] or f"{job['title']} finished.")
        emit(note + "\n", False)
    if result["status"] != 0 and not result["aborted"]:
        sys.exit(result["status"])


def _enqueue_background(cwd: str, workspace: str, job: dict, request: dict) -> None:
    append_log(cwd, job["id"], f"Starting {job['title']}.")
    append_log(cwd, job["id"], "Queued for background execution.")
    job.update(logFile=str(log_path(cwd, job["id"])), status="queued", phase="queued", request=request)

    broker = ensure_broker(cwd)
    if not broker:
        die("Could not start the opencode broker (opencode serve). Run /wh:setup to diagnose.")
    if not request.get("resume"):
        # Pre-create the opencode session so a queued job already has a
        # threadId and cancel never races session creation. Resume jobs pick
        # their thread in the worker (latest state at start time).
        thread_id = broker_create_session(broker, cwd, title=job.get("summary"))
        if not thread_id:
            die("The opencode broker did not create a session. Run /wh:setup to diagnose.")
        job["threadId"] = thread_id
        job["threadDir"] = cwd

    script = Path(__file__).resolve()
    runner = shutil.which("uv") or sys.executable
    runner_args = [runner, "run", str(script), "task-worker", "--job-id", job["id"], "--cwd", cwd]
    child = subprocess.Popen(
        runner_args, cwd=cwd, env=dict(os.environ), start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    job["pid"] = child.pid
    write_job(workspace, job["id"], job)
    upsert_job(cwd, job)
    # Detached worker: do not wait.


def cmd_task_worker(argv) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="wh-companion task-worker", add_help=False)
    p.add_argument("--job-id", required=True)
    p.add_argument("--cwd")
    args = p.parse_args(argv)
    cwd = args.cwd or os.getcwd()
    workspace = workspace_root(cwd)
    stored = read_job(workspace, args.job_id)
    if not stored:
        die(f"No stored job found for {args.job_id}.")
    if stored.get("status") == "cancelled":
        return  # cancelled while still queued
    req = stored.get("request") or {}

    broker = ensure_broker(cwd)
    if not broker:
        stored.update(status="failed", phase="failed", completedAt=now_iso(),
                      errorMessage="opencode broker unavailable")
        write_job(workspace, args.job_id, stored)
        upsert_job(cwd, stored)
        die("Could not start the opencode broker (opencode serve). Run /wh:setup to diagnose.")

    cand = resolve_resume_candidate(workspace, exclude_job=args.job_id) if req.get("resume") else None
    thread_id = (cand or {}).get("threadId") or stored.get("threadId")
    thread_dir = (cand or {}).get("threadDir") or stored.get("threadDir") or cwd
    if not thread_id:
        # Resume requested but nothing to resume (or an old queued job without
        # a pre-created session): start a fresh broker session.
        thread_id = broker_create_session(broker, cwd, title=stored.get("summary"))
        thread_dir = cwd
        cand = None
    if not thread_id:
        stored.update(status="failed", phase="failed", completedAt=now_iso(),
                      errorMessage="opencode broker did not create a session")
        write_job(workspace, args.job_id, stored)
        upsert_job(cwd, stored)
        die("The opencode broker did not create a session. Run /wh:setup to diagnose.")

    stored.update(status="running", phase="starting", pid=os.getpid(), threadId=thread_id,
                  threadDir=thread_dir, startedAt=now_iso(), logFile=str(log_path(cwd, args.job_id)))
    write_job(workspace, args.job_id, stored)
    upsert_job(cwd, stored)

    # A cancel may have landed while the worker was starting; honor it before
    # spawning the turn.
    if (read_job(workspace, args.job_id) or {}).get("status") == "cancelled":
        if not cand:
            broker_abort_session(broker, thread_dir, thread_id, timeout=2)
        return

    def _interrupted(signum, _frame) -> None:
        broker_abort_session(broker, thread_dir, thread_id, timeout=2)
        _mark_cancelled(cwd, workspace, args.job_id, "Cancelled: interrupted.")
        sys.exit(128 + int(signum))

    signal.signal(signal.SIGTERM, _interrupted)
    signal.signal(signal.SIGINT, _interrupted)

    progress = _progress_for(cwd, args.job_id, workspace)

    def on_text(txt: str) -> None:
        append_log(cwd, args.job_id, f"Assistant: {txt}")

    def on_start(proc_pid: int) -> None:
        upsert_job(cwd, {"id": args.job_id, "opencodePid": proc_pid})

    result = run_opencode_turn(cwd, req.get("prompt", ""), req.get("model"), req.get("variant"),
                               thread_id, bool(cand), broker["url"], on_text, progress, on_start)
    _finalize(cwd, workspace, stored, result)


def cmd_task_resume_candidate(argv) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="wh-companion task-resume-candidate", add_help=False)
    p.add_argument("--cwd")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    workspace = workspace_root(args.cwd or os.getcwd())
    jobs = sorted(current_session_jobs(list_jobs(workspace)), key=lambda j: j.get("updatedAt", ""), reverse=True)
    cand = next((j for j in jobs if j.get("jobClass") == "task" and j.get("threadId") and j["status"] not in ("queued", "running")), None)
    if args.json:
        payload = {
            "available": bool(cand),
            "sessionId": session_id(),
            "candidate": None if not cand else {
                "id": cand["id"], "status": cand["status"], "title": cand.get("title"),
                "summary": cand.get("summary"), "threadId": cand["threadId"],
                "completedAt": cand.get("completedAt"),
            },
        }
        print(json.dumps(payload, indent=2))
    else:
        print(f"Resumable task found: {cand['id']} ({cand['status']}).\n" if cand else "No resumable task found for this session.\n")


# --- setup -------------------------------------------------------------------


def cmd_setup(argv) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="wh-companion setup", add_help=False)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    wh = find_wh_binary()
    wh_detail = "not found"
    wh_authed = False
    if wh:
        who = run([wh, "whoami"], timeout=15)
        if who.returncode == 0:
            wh_authed = True
            wh_detail = f"{wh} (authenticated as {who.stdout.strip()})"
        else:
            wh_detail = f"{wh} (not authenticated)"

    oc = find_opencode_binary()
    oc_detail = run([oc, "--version"], timeout=10).stdout.strip() if oc else "not found"

    proxy_detail = "not running"
    if wh:
        pr = run([wh, "proxy"], timeout=15)
        out = pr.stdout + pr.stderr
        if pr.returncode == 0 and "Proxy running" in out:
            m = re.search(r"base_url:\s*(\S+)", out)
            proxy_detail = f"running at {m.group(1)}" if m else "running"

    providers_detail = "no workhorse-proxy models"
    if oc:
        models = run([oc, "models", "workhorse-proxy"], timeout=15)
        if models.returncode == 0:
            count = sum(1 for ln in models.stdout.splitlines() if ln.strip().startswith("workhorse-proxy/"))
            providers_detail = f"{count} workhorse-proxy model(s)" if count else providers_detail

    next_steps = []
    if not wh:
        next_steps.append("Install the wh CLI: curl -fsSL https://app.aiwork.horse/install/wh | bash")
    elif not wh_authed:
        next_steps.append("Authenticate the wh CLI: wh login")
    if not oc:
        next_steps.append("Install opencode from https://opencode.ai (or run 'wh attach <run>' / 'wh spectate').")
    if wh and "not running" in proxy_detail:
        next_steps.append("Start the Workhorse proxy: wh proxy on")
    if oc and "no workhorse" in providers_detail:
        next_steps.append("Configure opencode providers: wh opencode setup")

    ready = bool(wh and wh_authed and oc and "running" in proxy_detail and "model(s)" in providers_detail)
    report = {
        "ready": ready,
        "wh": {"available": bool(wh), "detail": wh_detail},
        "opencode": {"available": bool(oc), "detail": oc_detail},
        "proxy": {"detail": proxy_detail},
        "providers": {"detail": providers_detail},
        "nextSteps": next_steps,
    }
    if args.json:
        print(json.dumps(report, indent=2))
        return
    lines = [
        "# Workhorse Delegate Setup", "",
        f"Status: {'ready' if ready else 'needs attention'}", "",
        "Checks:",
        f"- wh CLI: {wh_detail}",
        f"- opencode: {oc_detail}",
        f"- proxy: {proxy_detail}",
        f"- providers: {providers_detail}",
    ]
    if next_steps:
        lines += ["", "Next steps:"] + [f"- {s}" for s in next_steps]
    print("\n".join(lines) + "\n")


# --- transfer ----------------------------------------------------------------


def _to_ms(v) -> int:
    if not v:
        return now_ms()
    if isinstance(v, (int, float)):
        return int(v)
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return now_ms()


def build_opencode_export_from_transcript(jsonl_path: str, cwd: str, title: str | None = None) -> dict:
    root = workspace_root(cwd)
    seq = 0

    def nid(prefix: str) -> str:
        nonlocal seq
        seq += 1
        return f"{prefix}_claude_{now_ms():x}_{seq}"

    session = f"ses_claude_{now_ms():x}_{os.urandom(3).hex()}"
    created = now_ms()
    messages: list[dict] = []
    pending: dict[str, dict] = {}
    last_msg: str | None = None
    first_user = ""

    for line in Path(jsonl_path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") in ("summary", "system"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role", entry.get("type"))
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            content = []
        ts = _to_ms(entry.get("timestamp"))

        if role == "user":
            texts: list[str] = []
            tool_only = True
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    pend = pending.pop(tid, None)
                    if pend:
                        result_text = block.get("content")
                        if isinstance(result_text, list):
                            result_text = "\n".join(b.get("text", "") for b in result_text if isinstance(b, dict))
                        pend["state"] = {"status": "completed", "input": pend["state"].get("input", {}),
                                         "output": str(result_text or "")[:8000], "title": pend["tool"],
                                         "metadata": {}, "time": {"start": ts, "end": ts}}
                    continue
                tool_only = False
                if isinstance(block, dict) and block.get("text"):
                    texts.append(block["text"])
            if tool_only:
                continue
            mid = nid("msg")
            if not first_user and texts:
                first_user = "\n".join(texts)
            messages.append({
                "info": {"id": mid, "sessionID": session, "role": "user", "time": {"created": ts},
                          "agent": "build", "model": {"providerID": "anthropic", "modelID": "claude"},
                          "summary": {"diffs": []}},
                "parts": [{"id": nid("prt"), "sessionID": session, "messageID": mid, "type": "text",
                           "text": t, "time": {"start": ts, "end": ts}} for t in texts],
            })
            last_msg = mid
            continue

        if role == "assistant":
            mid = nid("msg")
            parts: list[dict] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    parts.append({"id": nid("prt"), "sessionID": session, "messageID": mid, "type": "text",
                                 "text": block["text"], "time": {"start": ts, "end": ts}})
                elif block.get("type") == "tool_use":
                    tpart = {"id": nid("prt"), "sessionID": session, "messageID": mid, "type": "tool",
                             "callID": block.get("id", nid("prt")), "tool": block.get("name", "tool"),
                             "state": {"status": "running", "input": block.get("input", {}), "time": {"start": ts}},
                             "metadata": {}}
                    parts.append(tpart)
                    if block.get("id"):
                        pending[block["id"]] = tpart
            usage = message.get("usage") or {}
            messages.append({
                "info": {"id": mid, "sessionID": session, "role": "assistant",
                          "time": {"created": ts, "completed": ts}, "parentID": last_msg or mid,
                          "modelID": message.get("model") or "claude", "providerID": "anthropic",
                          "mode": "primary", "agent": "build", "path": {"cwd": cwd, "root": root},
                          "cost": 0, "tokens": {"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0),
                                                "reasoning": 0, "cache": {"read": 0, "write": 0}},
                          "finish": message.get("stop_reason")},
                "parts": parts,
            })
            last_msg = mid

    slug_base = re.sub(r"[^a-z0-9]+", "-", first_user.lower())[:14].strip("-") or "claude-transfer"
    info = {
        "id": session,
        "slug": f"{slug_base}-{os.urandom(2).hex()}",
        "projectID": "global",
        "directory": cwd,
        "title": title or shorten(first_user) or "Claude session transfer",
        "version": os.environ.get("OPENCODE_VERSION", "local"),
        "cost": 0,
        "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        "time": {"created": created, "updated": now_ms()},
        "permission": [{"permission": "question", "pattern": "*", "action": "deny"},
                        {"permission": "plan_enter", "pattern": "*", "action": "deny"},
                        {"permission": "plan_exit", "pattern": "*", "action": "deny"}],
    }
    return {"info": info, "messages": messages}


def resolve_transcript_path(cwd: str, source: str | None) -> str:
    requested = source or os.environ.get(TRANSCRIPT_PATH_ENV)
    if not requested:
        die("Could not identify the current Claude transcript. Retry with --source <path-to-claude-jsonl>.")
    p = Path(os.path.expanduser(requested))
    if not p.is_absolute():
        p = Path(cwd) / p
    if p.suffix != ".jsonl":
        die(f"Claude session source must be a JSONL file: {p}")
    if not p.exists():
        die(f"Claude session file not found: {p}")
    try:
        p.resolve().relative_to(PROJECTS_DIR)
    except ValueError:
        die(f"opencode can import Claude sessions only from {PROJECTS_DIR}: {p}")
    return str(p.resolve())


def cmd_transfer(argv) -> None:
    import argparse
    import tempfile

    p = argparse.ArgumentParser(prog="wh-companion transfer", add_help=False)
    p.add_argument("--source")
    p.add_argument("--cwd")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    cwd = args.cwd or os.getcwd()

    source = resolve_transcript_path(cwd, args.source)
    payload = build_opencode_export_from_transcript(source, cwd)
    if not payload["messages"]:
        die("The Claude transcript contained no transcribable messages.")

    tmp = Path(tempfile.mkdtemp(prefix="wh-transfer-")) / "opencode-session.json"
    tmp.write_text(json.dumps(payload, indent=2))

    oc = find_opencode_binary()
    if not oc:
        die("opencode is not installed. Run /wh:setup to diagnose.")
    result = run([oc, "import", str(tmp)], cwd=cwd)
    if result.returncode != 0:
        die(f"opencode import failed: {result.stderr.strip() or result.stdout.strip() or f'exit {result.returncode}'}")
    m = re.search(r"Imported session:\s*(\S+)", result.stdout)
    thread_id = m.group(1) if m else None
    if not thread_id:
        die(f"opencode import did not report a session id. Output: {result.stdout.strip()}")

    resume_cmd = f'opencode run --session {thread_id} --model workhorse-proxy/default "<follow-up prompt>"'
    tui_cmd = f"opencode --session {thread_id}"
    if args.json:
        print(json.dumps({"threadId": thread_id, "resumeCommand": resume_cmd, "tuiCommand": tui_cmd}, indent=2))
    else:
        emit(
            "Transferred the Claude session into an opencode session with visible turn history.\n"
            f"opencode session ID: {thread_id}\n"
            f"Resume in opencode: {resume_cmd}\n"
            f"Interactive TUI: {tui_cmd}\n",
            False,
        )


# --- status / result / cancel -----------------------------------------------


def _enrich(job: dict, max_lines: int = 4) -> dict:
    preview: list[str] = []
    lf = job.get("logFile")
    if lf and Path(lf).exists():
        preview = [
            re.sub(r"^\[[^\]]+\]\s*", "", ln.strip())
            for ln in Path(lf).read_text().splitlines()
            if ln.strip().startswith("[") and "Final output" not in ln and not ln.startswith("[") is False
        ]
        preview = [ln for ln in preview if not ln.startswith("Assistant:")][-max_lines:]

    def elapsed(start: str | None, end: str | None = None) -> str | None:
        if not start:
            return None
        try:
            s = time.mktime(time.strptime(start[:19], "%Y-%m-%dT%H:%M:%S"))
            e = time.time() if end is None else time.mktime(time.strptime(end[:19], "%Y-%m-%dT%H:%M:%S"))
            secs = max(0, int(e - s))
        except Exception:
            return None
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")

    return {
        **job,
        "kindLabel": job.get("kindLabel", "task"),
        "progressPreview": preview if job["status"] in ("queued", "running", "failed") else [],
        "elapsed": elapsed(job.get("startedAt", job.get("createdAt"))) if job["status"] in ("queued", "running") else None,
        "duration": elapsed(job.get("startedAt", job.get("createdAt")), job.get("completedAt") or job.get("updatedAt"))
        if job["status"] in ("completed", "failed", "cancelled") else None,
    }


def _find_job(jobs: list[dict], ref: str, predicate=None) -> dict | None:
    filtered = [j for j in jobs if predicate is None or predicate(j)]
    if not ref:
        return filtered[0] if filtered else None
    exact = next((j for j in filtered if j["id"] == ref), None)
    if exact:
        return exact
    prefix = [j for j in filtered if j["id"].startswith(ref)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise ValueError(f'Job reference "{ref}" is ambiguous. Use a longer job id.')
    raise ValueError(f'No job found for "{ref}". Run /wh:status to list known jobs.')


def cmd_status(argv) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="wh-companion status", add_help=False)
    p.add_argument("job_id", nargs="?")
    p.add_argument("--cwd")
    p.add_argument("--all", action="store_true")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout-ms", type=int)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    cwd = args.cwd or os.getcwd()
    workspace = workspace_root(cwd)

    if args.job_id:
        deadline = (now_ms() + (args.timeout_ms or DEFAULT_WAIT_TIMEOUT_MS)) if args.wait else 0
        job = None
        while True:
            jobs = sorted(list_jobs(workspace), key=lambda j: j.get("updatedAt", ""), reverse=True)
            try:
                job = _find_job(jobs, args.job_id)
            except ValueError as e:
                die(str(e))
            if not args.wait or not job or job["status"] not in ("queued", "running") or now_ms() > deadline:
                break
            time.sleep(DEFAULT_POLL_MS / 1000)
        snapshot = {"workspaceRoot": workspace, "job": _enrich(job)}
        emit(snapshot, args.json) if args.json else emit(_render_job_status(snapshot["job"]), False)
        return

    if args.wait:
        die("`status --wait` requires a job id.")
    jobs = sorted(list_jobs(workspace), key=lambda j: j.get("updatedAt", ""), reverse=True)
    scoped = jobs if args.all else current_session_jobs(jobs)
    running = [_enrich(j) for j in scoped if j["status"] in ("queued", "running")]
    finished = [j for j in scoped if j["status"] not in ("queued", "running")]
    latest = _enrich(finished[0]) if finished else None
    recent = [_enrich(j) for j in finished[1:8]] if finished else []
    report = {"workspaceRoot": workspace, "running": running, "latestFinished": latest, "recent": recent}
    emit(report, args.json) if args.json else emit(_render_status(report), False)


def cmd_result(argv) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="wh-companion result", add_help=False)
    p.add_argument("job_id", nargs="?")
    p.add_argument("--cwd")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    cwd = args.cwd or os.getcwd()
    workspace = workspace_root(cwd)
    jobs = sorted(list_jobs(workspace), key=lambda j: j.get("updatedAt", ""), reverse=True)
    pool = jobs if args.job_id else current_session_jobs(jobs)
    try:
        job = _find_job(pool, args.job_id or "", lambda j: j["status"] in ("completed", "failed", "cancelled"))
    except ValueError as e:
        die(str(e))
    if not job:
        active = _find_job(jobs, args.job_id or "", lambda j: j["status"] in ("queued", "running"))
        if active:
            die(f"Job {active['id']} is still {active['status']}. Check /wh:status and try again once it finishes.")
        die("No finished opencode jobs found for this repository yet.")
    stored = read_job(workspace, job["id"]) or {}
    emit({"job": job, "storedJob": stored}, args.json) if args.json else emit(_render_result(job, stored), False)


def cmd_cancel(argv) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="wh-companion cancel", add_help=False)
    p.add_argument("job_id", nargs="?")
    p.add_argument("--cwd")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    cwd = args.cwd or os.getcwd()
    workspace = workspace_root(cwd)
    jobs = sorted(list_jobs(workspace), key=lambda j: j.get("updatedAt", ""), reverse=True)
    active = [j for j in current_session_jobs(jobs) if j["status"] in ("queued", "running")]
    if not active:
        die("No active opencode jobs to cancel.")
    try:
        job = _find_job(active, args.job_id or "")
    except ValueError as e:
        die(str(e))
    if not job:
        die("No active opencode jobs to cancel.")
    # Preferred path: abort the turn through the broker so opencode stops it
    # itself — the session is flushed, marked idle, and stays resumable, and
    # the worker records cancelled (not failed).
    stored = read_job(workspace, job["id"]) or {}
    thread_id = job.get("threadId") or stored.get("threadId")
    thread_dir = job.get("threadDir") or stored.get("threadDir") or cwd
    broker = read_broker(cwd)
    aborted = False
    if broker and thread_id and _pid_alive(broker.get("pid")) and broker_healthy(broker["url"], timeout=2):
        aborted = broker_abort_session(broker, thread_dir, thread_id, timeout=3)
    if aborted:
        append_log(cwd, job["id"], "Turn aborted through the opencode broker.")
        if not job.get("opencodePid"):
            # The worker may still be setting up the turn (CLI not spawned yet
            # at first abort). Abort once more after a short grace period to
            # catch a turn that started just after the first abort.
            time.sleep(2)
            broker_abort_session(broker, thread_dir, thread_id, timeout=3)
    else:
        # Last resort (broker unreachable, or a queued job whose worker never
        # started a turn): stop the process group directly.
        kill_pid = job.get("opencodePid") or job.get("pid")
        if kill_pid:
            try:
                os.killpg(kill_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    os.kill(kill_pid, signal.SIGTERM)
                except Exception:
                    pass
        append_log(cwd, job["id"], "Cancelled by user (process stop; broker unavailable).")
    append_log(cwd, job["id"], "Cancelled by user.")
    _mark_cancelled(cwd, workspace, job["id"], "Cancelled by user.")
    note = f"\n- The opencode session {thread_id} was stopped cleanly and can be resumed." if aborted and thread_id else ""
    emit({"jobId": job["id"], "status": "cancelled", "title": job.get("title"), "aborted": aborted,
          "threadId": thread_id}, args.json) if args.json else emit(
        f"# Workhorse Delegate Cancel\n\nCancelled {job['id']}.{note}\n\n- Check `/wh:status` for the updated queue.\n", False
    )


def cmd_broker(argv) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="wh-companion broker", add_help=False)
    p.add_argument("--stop", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--cwd")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    cwd = args.cwd or os.getcwd()

    if args.stop:
        remaining = lease_count(cwd)
        if remaining and not args.force:
            die(f"{remaining} Claude session(s) still hold a broker lease. Use --force to stop anyway.")
        stopped = stop_broker(cwd)
        emit({"stopped": stopped}, args.json) if args.json else emit(
            "Broker stopped.\n" if stopped else "No running broker found.\n", False)
        return

    broker = read_broker(cwd)
    health = broker_healthy(broker["url"]) if broker else None
    _, blog, _ = _broker_paths(cwd)
    payload = {
        "running": bool(broker and health),
        "url": broker.get("url") if broker else None,
        "pid": broker.get("pid") if broker else None,
        "startedAt": broker.get("startedAt") if broker else None,
        "version": health.get("version") if health else None,
        "leases": lease_count(cwd),
        "logFile": str(blog),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        lines = ["# Workhorse Delegate Broker", ""]
        if payload["running"]:
            lines += [f"Status: running (opencode {payload.get('version') or 'unknown'})",
                      f"URL: {payload['url']}", f"PID: {payload['pid']}",
                      f"Started: {payload.get('startedAt')}", f"Active Claude session leases: {payload['leases']}",
                      f"Log: {payload['logFile']}"]
        else:
            lines.append("Status: not running (starts on demand with the first task)")
        emit("\n".join(lines) + "\n", False)


# --- renderers ---------------------------------------------------------------


def _render_job_status(job: dict) -> str:
    lines = ["# Workhorse Delegate Job Status", "", f"- {job['id']} | {job['status']} | {job.get('kindLabel', 'task')} | {job.get('title', '')}"]
    if job.get("summary"):
        lines.append(f"  Summary: {job['summary']}")
    if job.get("phase"):
        lines.append(f"  Phase: {job['phase']}")
    if job.get("elapsed"):
        lines.append(f"  Elapsed: {job['elapsed']}")
    if job.get("duration"):
        lines.append(f"  Duration: {job['duration']}")
    if job.get("threadId"):
        lines.append(f"  opencode session ID: {job['threadId']}")
        lines.append(f"  Resume in opencode: opencode run --session {job['threadId']}")
    if job.get("logFile"):
        lines.append(f"  Log: {job['logFile']}")
    if job["status"] in ("queued", "running"):
        lines.append(f"  Cancel: /wh:cancel {job['id']}")
    elif job["status"] in ("completed", "failed", "cancelled"):
        lines.append(f"  Result: /wh:result {job['id']}")
    if job.get("progressPreview"):
        lines.append("  Progress:")
        lines += [f"    {ln}" for ln in job["progressPreview"]]
    return "\n".join(lines) + "\n"


def _render_status(report: dict) -> str:
    lines = ["# Workhorse Delegate Status", ""]
    running = report.get("running", [])
    if running:
        lines += ["Active jobs:",
                  "| Job | Kind | Status | Phase | Elapsed | opencode Session ID | Summary | Actions |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for j in running:
            actions = f"`/wh:status {j['id']}`<br>`/wh:cancel {j['id']}`"
            lines.append(f"| {j['id']} | {j.get('kindLabel','task')} | {j['status']} | {j.get('phase','')} | {j.get('elapsed','')} | {j.get('threadId','')} | {j.get('summary','')} | {actions} |")
        lines.append("")
    latest = report.get("latestFinished")
    if latest:
        lines.append(f"Latest finished: {latest['id']} | {latest['status']} | {latest.get('summary','')}")
        if latest.get("threadId"):
            lines.append(f"  opencode session ID: {latest['threadId']}")
        lines.append("")
    recent = report.get("recent", [])
    if recent:
        lines.append("Recent jobs:")
        for j in recent:
            lines.append(f"  - {j['id']} | {j['status']} | {j.get('summary','')}")
    elif not running and not latest:
        lines.append("No jobs recorded yet.")
    return "\n".join(lines).rstrip() + "\n"


def _render_result(job: dict, stored: dict) -> str:
    tid = stored.get("threadId") or job.get("threadId")
    raw = (stored.get("result") or {}).get("rawOutput") or ""
    if raw:
        out = raw if raw.endswith("\n") else raw + "\n"
        if tid:
            out += f"\nopencode session ID: {tid}\nResume in opencode: opencode run --session {tid}\n"
        return out
    error = (stored.get("result") or {}).get("error") or job.get("errorMessage") or stored.get("errorMessage")
    if error:
        return f"{error}\n"
    return f"# {job.get('title', 'opencode Result')}\n\nJob: {job['id']}\nStatus: {job['status']}\n\nNo captured result payload was stored for this job.\n"


# --- main --------------------------------------------------------------------

USAGE = """Usage:
  uv run wh-companion.py setup [--json]
  uv run wh-companion.py task [--background] [--resume|--fresh] [--model <m>] [--variant <v>] [prompt]
  uv run wh-companion.py task-worker --job-id <id> [--cwd <dir>]
  uv run wh-companion.py task-resume-candidate [--json]
  uv run wh-companion.py transfer [--source <claude-jsonl>] [--json]
  uv run wh-companion.py status [job-id] [--all] [--wait] [--timeout-ms <ms>] [--json]
  uv run wh-companion.py result [job-id] [--json]
  uv run wh-companion.py cancel [job-id] [--json]
  uv run wh-companion.py broker [--stop [--force]] [--json]
"""

HANDLERS = {
    "setup": cmd_setup,
    "task": cmd_task,
    "task-worker": cmd_task_worker,
    "task-resume-candidate": cmd_task_resume_candidate,
    "transfer": cmd_transfer,
    "status": cmd_status,
    "result": cmd_result,
    "cancel": cmd_cancel,
    "broker": cmd_broker,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("help", "--help", "-h"):
        print(USAGE)
        return
    cmd, rest = sys.argv[1], sys.argv[2:]
    handler = HANDLERS.get(cmd)
    if not handler:
        die(f"Unknown subcommand: {cmd}\n\n{USAGE}")
    handler(rest)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        err(f"{e}")
        sys.exit(1)
