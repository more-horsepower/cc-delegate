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
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
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
        "threadId", "startedAt", "completedAt", "logFile", "errorMessage",
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


def reap_session_jobs(cwd: str, sid: str) -> int:
    """Kill active jobs for `sid` and drop them from the index. Returns the count reaped."""
    if not cwd or not sid:
        return 0
    jobs = list_jobs(cwd)
    reaped = 0
    for j in jobs:
        if j.get("sessionId") == sid and j["status"] in ("queued", "running"):
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
            reaped += 1
    if reaped:
        _save_index(cwd, [j for j in jobs if not (j.get("sessionId") == sid and j["status"] in ("queued", "running"))])
    return reaped


def current_session_jobs(jobs: list[dict]) -> list[dict]:
    sid = session_id()
    return [j for j in jobs if not sid or j.get("sessionId") == sid]


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


def run_opencode_turn(cwd, prompt, model, variant, resume_id, on_text, on_progress, on_start=None):
    """Spawn `opencode run --format json`, stream text, return (status, sid, raw, error).

    `on_start(proc_pid)` is called right after the opencode process is spawned
    so callers can record the opencode process-group leader for cancellation."""
    binary = find_opencode_binary()
    if not binary:
        return 1, None, "", "opencode binary not found. Run /wh:setup to diagnose."

    effective = prompt or (DEFAULT_CONTINUE_PROMPT if resume_id else "")
    if not effective and not resume_id:
        return 1, None, "", "Provide a prompt or use --resume."

    args = [binary, "run"]
    if effective:
        args.append(effective)
    args += ["--dir", cwd, "--format", "json", "--auto"]
    if model:
        args += ["--model", model]
    if variant:
        args += ["--variant", variant]
    if resume_id:
        args += ["--session", resume_id]

    try:
        proc = subprocess.Popen(
            args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
    except FileNotFoundError:
        return 1, None, "", "opencode binary not found."

    if on_start:
        on_start(proc.pid)

    sid = None
    texts: list[str] = []
    failure = ""

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
            failure = f"{failure}\n{msg}" if failure else msg
            on_progress(f"opencode error: {msg}", "failed")

    proc.wait()
    stderr = proc.stderr.read().strip()
    raw = "".join(texts)
    status = 1 if failure else proc.returncode
    error = failure or (stderr if status else "")
    return status, sid, raw, error


# --- task metadata / resume --------------------------------------------------


def task_metadata(prompt: str, resume: bool) -> dict:
    title = "opencode Resume" if resume else "opencode Task"
    summary = shorten(prompt or (DEFAULT_CONTINUE_PROMPT if resume else "Task"))
    return {"title": title, "summary": summary}


def resolve_resume_thread(workspace: str, exclude_job: str | None = None) -> str | None:
    jobs = sorted(current_session_jobs(list_jobs(workspace)), key=lambda j: j.get("updatedAt", ""), reverse=True)
    if exclude_job:
        jobs = [j for j in jobs if j["id"] != exclude_job]
    active = next((j for j in jobs if j.get("jobClass") == "task" and j["status"] in ("queued", "running")), None)
    if active:
        die(f"Task {active['id']} is still running. Use /wh:status before continuing it.")
    cand = next(
        (j for j in jobs if j.get("jobClass") == "task" and j.get("threadId") and j["status"] not in ("queued", "running")),
        None,
    )
    return cand["threadId"] if cand else None


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


def _finalize(cwd: str, workspace: str, job: dict, status: int, sid: str | None, raw: str, error: str) -> None:
    done = "completed" if status == 0 else "failed"
    job.update(status=done, phase=done, threadId=sid, pid=None, completedAt=now_iso(),
               result={"status": status, "threadId": sid, "rawOutput": raw, "error": error})
    write_job(workspace, job["id"], job)
    upsert_job(cwd, job)


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

    resume_id = resolve_resume_thread(workspace) if args.resume else None
    if not prompt and not resume_id:
        die("Provide a prompt, a prompt file, piped stdin, or use --resume/--resume-last.")

    _track_running(cwd, workspace, job)
    progress = _progress_for(cwd, job["id"], workspace)

    def on_text(txt: str) -> None:
        if not args.json:
            sys.stdout.write(txt + "\n")
            sys.stdout.flush()
        append_log(cwd, job["id"], f"Assistant: {txt}")

    def on_start(proc_pid: int) -> None:
        upsert_job(cwd, {"id": job["id"], "opencodePid": proc_pid})

    try:
        status, sid, raw, error = run_opencode_turn(cwd, prompt, model, args.variant, resume_id, on_text, progress, on_start)
    except Exception as e:
        _finalize(cwd, workspace, job, 1, None, "", str(e))
        raise
    _finalize(cwd, workspace, job, status, sid, raw, error)
    if args.json:
        print(json.dumps(job["result"], indent=2))
    elif not raw:
        emit((error or f"{job['title']} finished.") + "\n", False)
    if status != 0:
        sys.exit(status)


def _enqueue_background(cwd: str, workspace: str, job: dict, request: dict) -> None:
    append_log(cwd, job["id"], f"Starting {job['title']}.")
    append_log(cwd, job["id"], "Queued for background execution.")
    job.update(logFile=str(log_path(cwd, job["id"])), status="queued", phase="queued", request=request)

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
    req = stored.get("request") or {}
    resume_id = resolve_resume_thread(workspace, exclude_job=args.job_id) if req.get("resume") else None

    stored.update(status="running", phase="starting", pid=os.getpid(), startedAt=now_iso(), logFile=str(log_path(cwd, args.job_id)))
    write_job(workspace, args.job_id, stored)
    upsert_job(cwd, stored)
    progress = _progress_for(cwd, args.job_id, workspace)

    def on_text(txt: str) -> None:
        append_log(cwd, args.job_id, f"Assistant: {txt}")

    def on_start(proc_pid: int) -> None:
        upsert_job(cwd, {"id": args.job_id, "opencodePid": proc_pid})

    status, sid, raw, error = run_opencode_turn(cwd, req.get("prompt", ""), req.get("model"), req.get("variant"), resume_id, on_text, progress, on_start)
    done = "completed" if status == 0 else "failed"
    stored.update(status=done, phase=done, threadId=sid, pid=None, completedAt=now_iso(),
                  result={"status": status, "threadId": sid, "rawOutput": raw, "error": error})
    write_job(workspace, args.job_id, stored)
    upsert_job(cwd, stored)


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
    # Prefer the opencode process-group leader (set when opencode was spawned);
    # fall back to the worker pid for jobs that never started opencode (queued).
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
    append_log(cwd, job["id"], "Cancelled by user.")
    done = now_iso()
    stored = read_job(workspace, job["id"]) or {}
    stored.update(status="cancelled", phase="cancelled", pid=None, completedAt=done, errorMessage="Cancelled by user.")
    write_job(workspace, job["id"], stored)
    upsert_job(cwd, {"id": job["id"], "status": "cancelled", "phase": "cancelled", "pid": None, "completedAt": done})
    emit({"jobId": job["id"], "status": "cancelled", "title": job.get("title")}, args.json) if args.json else emit(
        f"# Workhorse Delegate Cancel\n\nCancelled {job['id']}.\n\n- Check `/wh:status` for the updated queue.\n", False
    )


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
