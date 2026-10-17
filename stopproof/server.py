"""Demo API + static UI (Starlette). Runs the real pipeline: Nemotron + an execution backend."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from . import benchmark, escalation, livedemo
from .model import ModelError, Nemotron
from .orchestrator import Pipeline
from .sandbox import BACKENDS, SandboxUnavailable, backend_status, make_sandbox
from .verifier import reverify_bundle, verify

log = logging.getLogger("stopproof.server")
MAX_TRACE_BYTES = 1_000_000
UI = Path(__file__).parent / "ui" / "index.html"
DEMO_DIR = Path("demo")
RUN_ID = re.compile(r"^[A-Za-z0-9_-]+$")
NTFY_TOPIC = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
NTFY_MESSAGES = "https://ntfy.sh/{topic}/json?poll=1"


def create_app(runs_dir: Path) -> Starlette:
    jobs: dict[str, dict[str, Any]] = {}

    def repos() -> dict[str, Path]:
        return {p.name: p for p in sorted(DEMO_DIR.iterdir()) if p.is_dir()} if DEMO_DIR.is_dir() else {}

    def stored() -> list[dict[str, Any]]:
        return benchmark.load_evidence(runs_dir) if runs_dir.is_dir() else []

    def probe() -> dict[str, Any]:
        load_dotenv(override=True)  # lets "re-check" pick up a changed .env without a restart
        out: dict[str, Any] = {"repos": list(repos()), "backends": backend_status()}
        try:
            out["model"] = {"ok": True, "name": Nemotron(cache=False).model}
        except ModelError as exc:
            out["model"] = {"ok": False, "error": str(exc)}
        ready = [name for name, s in out["backends"].items() if s["ok"]]
        out["default_backend"] = ready[0] if ready else None
        return out

    async def index(_: Request) -> FileResponse:
        return FileResponse(UI, headers={"Cache-Control": "no-store"})  # the UI changes with the code; never serve a stale copy

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(await run_in_threadpool(probe))

    def work(job_id: str, repo: Path, backend: str) -> None:
        job = jobs[job_id]
        try:
            runner = make_sandbox(backend)
            pipeline = Pipeline(repo, Nemotron(), runner, runs_dir, on_event=lambda e: job["stages"].append(e))
            job["evidence"] = pipeline.run()
            job["status"] = "done"
        except SandboxUnavailable as exc:  # raised before any model tokens are spent
            job.update(status="error", error=str(exc), code=exc.code)
        except ModelError as exc:
            job.update(status="error", error=str(exc), code="model_unavailable")
        except Exception as exc:
            log.exception("job_failed job=%s", job_id)
            job.update(status="error", error=f"{type(exc).__name__}: {exc}", code="internal")

    async def start(request: Request) -> JSONResponse:
        body = await request.json()
        repo, backend = repos().get(body.get("repo")), body.get("backend") or os.environ.get("STOPPROOF_SANDBOX", "nebius")
        if repo is None:
            return JSONResponse({"error": f"unknown repository {body.get('repo')!r}"}, status_code=400)
        if backend not in BACKENDS:
            return JSONResponse({"error": f"unknown backend {backend!r}; choose one of {sorted(BACKENDS)}"}, status_code=400)
        job_id = uuid.uuid4().hex[:8]
        jobs[job_id] = {"status": "running", "stages": [], "repo": repo.name, "backend": backend}
        threading.Thread(target=work, args=(job_id, repo, backend), daemon=True).start()
        return JSONResponse({"job": job_id})

    async def job(request: Request) -> JSONResponse:
        state = jobs.get(request.path_params["job_id"])
        if state is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        return JSONResponse(state)

    async def evidence_list(_: Request) -> JSONResponse:
        rows = [{"run_id": e["run_id"], "repo": e.get("repository", {}).get("path"), "outcome": e["verdict"]["outcome"], "patched": bool(e.get("patch", {}).get("applied")),
                 "error": bool(e.get("error")), "backend": e.get("execution", {}).get("backend") or e.get("baseline", {}).get("sandbox", {}).get("backend")} for e in stored()]
        return JSONResponse(rows)

    def load(run_id: str) -> dict[str, Any] | None:
        path = runs_dir / run_id / "evidence.json"
        return json.loads(path.read_text()) if RUN_ID.match(run_id) and path.exists() else None

    async def evidence(request: Request) -> JSONResponse:
        data = load(request.path_params["run_id"])
        return JSONResponse(data) if data else JSONResponse({"error": "unknown run"}, status_code=404)

    async def reverify(request: Request) -> JSONResponse:
        data = load(request.path_params["run_id"])
        return JSONResponse(reverify_bundle(data)) if data else JSONResponse({"error": "unknown run"}, status_code=404)

    async def metrics(_: Request) -> JSONResponse:
        return JSONResponse(benchmark.score(stored()))

    async def verify_trace(request: Request) -> JSONResponse:
        """Runs the deterministic verifier over a trace posted by the UI's trace editor."""
        raw = await request.body()
        if len(raw) > MAX_TRACE_BYTES:
            return JSONResponse({"error": "trace is too large"}, status_code=413)
        try:
            trace = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": 'body must be a JSON trace: {"run_id": "...", "events": [...]}'}, status_code=400)
        return JSONResponse(verify(trace).to_dict())

    def live_work(job_id: str, targets: list[Path], sink: str, topic: str) -> None:
        job = jobs[job_id]
        try:
            for repo in targets:
                job["stages"].append({"stage": "live", "status": "running", "repo": repo.name})
                res = livedemo.run_live(repo, sink)
                res["self_checks"] = livedemo.run_selfcheck(repo)
                res["topic"] = topic
                job["variants"].append(res)
            job["status"] = "done"
        except Exception as exc:
            log.exception("live_job_failed job=%s", job_id)
            job.update(status="error", error=f"{type(exc).__name__}: {exc}", code="internal")

    async def start_live(request: Request) -> JSONResponse:
        """Live demo: the same races on the host with real outbound sinks (ntfy push, optional SMTP email), A/B: broken then fixed."""
        available = repos()
        targets = [available[name] for name in ("vulnerable_agent", "fixed_agent") if name in available]
        if not targets:
            return JSONResponse({"error": "no live-capable demo repositories found"}, status_code=400)
        job_id = uuid.uuid4().hex[:8]
        topic = f"stopproof-{job_id}{uuid.uuid4().hex[:6]}"
        sinks = [f"https://ntfy.sh/{topic}"]
        email_to = os.environ.get("STOPPROOF_EMAIL")
        if email_to:
            sinks.append(f"mailto:{email_to}")
        jobs[job_id] = {"status": "running", "stages": [{"stage": "live", "status": "running"}], "backend": "live-host", "topic": topic, "variants": [], "email_to": email_to}
        threading.Thread(target=live_work, args=(job_id, targets, ",".join(sinks), topic), daemon=True).start()
        return JSONResponse({"job": job_id, "topic": topic, "subscribe": f"https://ntfy.sh/{topic}", "email_to": email_to})

    async def live_messages(request: Request) -> JSONResponse:
        """Server-side poll of an ntfy topic so the browser inbox works without CORS or setup."""
        topic = request.path_params["topic"]
        if not NTFY_TOPIC.match(topic):
            return JSONResponse({"error": "invalid topic"}, status_code=400)
        import urllib.request

        def fetch() -> list[dict[str, Any]]:
            req = urllib.request.Request(NTFY_MESSAGES.format(topic=topic), headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                messages = []
                for line in resp.read().decode().splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") == "message":
                        messages.append({"time": event.get("time"), "message": event.get("message", ""), "title": event.get("title", "")})
                return messages

        try:
            return JSONResponse({"messages": await run_in_threadpool(fetch)})
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": f"ntfy unreachable: {type(exc).__name__}"}, status_code=502)

    async def escalate(request: Request) -> JSONResponse:
        """Hand an inconclusive run to a Tendem human expert. Opt-in per run; never changes the verdict."""
        body = await request.json()
        data = load(str(body.get("run_id", "")))
        if data is None:
            return JSONResponse({"error": "unknown run"}, status_code=404)
        if data.get("verdict", {}).get("outcome") != "CANNOT ESTABLISH":
            return JSONResponse({"error": "escalation exists for inconclusive (CANNOT ESTABLISH) runs"}, status_code=400)
        if not escalation.enabled():
            return JSONResponse({"error": "TENDEM_API_KEY is not configured"}, status_code=400)
        try:
            result = await run_in_threadpool(escalation.request_human_review, data)
        except escalation.EscalationUnavailable as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        (runs_dir / str(body["run_id"]) / "escalation.json").write_text(json.dumps(result, indent=2))
        return JSONResponse(result)

    return Starlette(routes=[
        Route("/", index), Route("/api/health", health), Route("/api/runs", start, methods=["POST"]), Route("/api/runs/{job_id}", job),
        Route("/api/evidence", evidence_list), Route("/api/evidence/{run_id}", evidence), Route("/api/evidence/{run_id}/reverify", reverify), Route("/api/metrics", metrics),
        Route("/api/verify", verify_trace, methods=["POST"]),
        Route("/api/live-demo", start_live, methods=["POST"]), Route("/api/live/{topic}/messages", live_messages),
        Route("/api/escalate", escalate, methods=["POST"]),
    ])
