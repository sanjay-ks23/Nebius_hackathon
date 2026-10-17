"""Demo API + static UI (Starlette). Runs the real pipeline: Nemotron + an execution backend."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
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
MAX_DELIVER_BYTES = 4_000
MAX_INBOX = 100
MAX_JOBS = 256
INBOX_TTL = int(os.environ.get("STOPPROOF_INBOX_TTL", str(2 * 3600)))  # live inboxes expire after this many seconds
DELIVER_RATE = (int(os.environ.get("STOPPROOF_DELIVER_RATE", "120")), 60)  # max deliveries per IP per window
RUN_RATE = (int(os.environ.get("STOPPROOF_RUN_RATE", "6")), 60)  # max runs per IP per window (seconds)
LIVE_RATE = (int(os.environ.get("STOPPROOF_LIVE_RATE", "6")), 60)  # max live demos per IP per window
MAX_RUNNING = int(os.environ.get("STOPPROOF_MAX_RUNNING", "4"))  # global cap on in-flight jobs


def create_app(runs_dir: Path) -> Starlette:
    jobs: dict[str, dict[str, Any]] = {}
    inboxes: dict[str, list[dict[str, Any]]] = {}
    buckets: dict[str, list[float]] = {}
    lock = threading.Lock()

    def client_ip(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        return fwd or (request.client.host if request.client else "unknown")

    def rate_limited(request: Request, limit: int, window: int) -> bool:
        now = time.time()
        with lock:
            hits = [t for t in buckets.get(client_ip(request), []) if now - t < window]
            if len(hits) >= limit:
                return True
            hits.append(now)
            buckets[client_ip(request)] = hits
            return False

    def busy() -> bool:
        return sum(1 for j in jobs.values() if j.get("status") == "running") >= MAX_RUNNING

    def new_job(job_id: str, record: dict[str, Any]) -> None:
        while len(jobs) >= MAX_JOBS:
            victim = next((k for k, v in jobs.items() if v.get("status") != "running"), None)
            jobs.pop(victim if victim else next(iter(jobs)))
        jobs[job_id] = record

    def public_base(request: Request) -> str:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.url.netloc)
        return f"{proto}://{host}"

    def repos() -> dict[str, Path]:
        return {p.name: p for p in sorted(DEMO_DIR.iterdir()) if p.is_dir() and not p.name.startswith((".", "_"))} if DEMO_DIR.is_dir() else {}

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
        if rate_limited(request, *RUN_RATE):
            return JSONResponse({"error": "rate limit — too many runs from this address; try again shortly"}, status_code=429)
        if busy():
            return JSONResponse({"error": "server is at capacity — a run is already in progress; try again shortly"}, status_code=429)
        job_id = uuid.uuid4().hex[:8]
        new_job(job_id, {"status": "running", "stages": [], "repo": repo.name, "backend": backend})
        threading.Thread(target=work, args=(job_id, repo, backend), daemon=True).start()
        return JSONResponse({"job": job_id})

    async def job(request: Request) -> JSONResponse:
        state = jobs.get(request.path_params["job_id"])
        if state is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        return JSONResponse({k: v for k, v in state.items() if not k.startswith("_")})

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
            inboxes[topic]["msgs"].append({
                "time": time.time(), "title": "StopProof",
                "message": "live proof starting — sends the agent performs will appear here",
            })
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
        """Live demo: the same races on the host with real outbound sinks — this app's own
        deliver endpoint plus a local file inbox (ntfy fan-out only if STOPPROOF_NTFY=1).
        Three agents: broken (acts anyway), fixed (provably stops), opaque (can't prove either way)."""
        if rate_limited(request, *LIVE_RATE):
            return JSONResponse({"error": "rate limit — too many live runs from this address; try again shortly"}, status_code=429)
        if busy():
            return JSONResponse({"error": "server is at capacity — a run is already in progress; try again shortly"}, status_code=429)
        body = await request.json() if (await request.body()) else {}
        available = repos()
        want = str(body.get("repo") or "").strip()
        if want and want not in available:
            return JSONResponse({"error": f"unknown repository {want!r}"}, status_code=400)
        targets = [available[want]] if want else [available[name] for name in ("vulnerable_agent", "fixed_agent", "opaque_agent") if name in available]
        if not targets:
            return JSONResponse({"error": "no live-capable demo repositories found"}, status_code=400)
        job_id = uuid.uuid4().hex[:8]
        topic = f"stopproof-{job_id}{uuid.uuid4().hex[:6]}"
        sink_dir = (runs_dir / f"live-{job_id}").resolve()
        sinks = [f"{public_base(request)}/api/live/{topic}/deliver", f"file://{sink_dir}/"]
        if os.environ.get("STOPPROOF_NTFY"):
            sinks.append(f"https://ntfy.sh/{topic}")
        while len(inboxes) >= MAX_JOBS:
            live = {j.get("topic") for j in jobs.values() if j.get("status") == "running"}
            victim = next((k for k in inboxes if k not in live), None)
            if victim is None:
                break
            inboxes.pop(victim)
        inboxes[topic] = {"created": time.time(), "msgs": []}
        new_job(job_id, {"status": "running", "stages": [{"stage": "live", "status": "running"}], "backend": "live-host", "topic": topic, "variants": [], "_sink_dir": str(sink_dir)})
        threading.Thread(target=live_work, args=(job_id, targets, ",".join(sinks), topic), daemon=True).start()
        return JSONResponse({"job": job_id, "topic": topic, "subscribe": f"/live/{topic}"})

    def inbox(topic: str) -> dict[str, Any] | JSONResponse:
        """Resolve a live inbox, enforcing the topic syntax, existence, and TTL."""
        if not NTFY_TOPIC.match(topic):
            return JSONResponse({"error": "invalid topic"}, status_code=400)
        box = inboxes.get(topic)
        if box is None:
            return JSONResponse({"error": "unknown topic"}, status_code=404)
        if time.time() - box["created"] > INBOX_TTL:
            inboxes.pop(topic, None)
            return JSONResponse({"error": "this live inbox has expired — run the live proof again for a fresh link"}, status_code=410)
        return box

    async def live_deliver(request: Request) -> JSONResponse:
        """The real outbound sink for live runs: the repo's provider POSTs each send here.
        Only accepts topics this app created (unguessable ids), size-, count-, and rate-capped."""
        if rate_limited(request, *DELIVER_RATE):
            return JSONResponse({"error": "rate limit"}, status_code=429)
        box = inbox(request.path_params["topic"])
        if isinstance(box, JSONResponse):
            return box
        raw = await request.body()
        if len(raw) > MAX_DELIVER_BYTES:
            return JSONResponse({"error": "message too large"}, status_code=413)
        if len(box["msgs"]) >= MAX_INBOX:
            return JSONResponse({"error": "inbox is full"}, status_code=429)
        box["msgs"].append({"time": time.time(), "message": raw.decode(errors="replace").strip()[:2000], "title": "delivered"})
        return JSONResponse({"ok": True})

    async def live_messages(request: Request) -> JSONResponse:
        """The inbox for a live topic: own deliveries merged with the local file sink."""
        box = inbox(request.path_params["topic"])
        if isinstance(box, JSONResponse):
            return box
        topic = request.path_params["topic"]
        sink_dir = next((j.get("_sink_dir") for j in jobs.values() if j.get("topic") == topic), None)
        messages = list(box["msgs"])
        if sink_dir:
            d = Path(sink_dir)
            if d.is_dir():
                for f in sorted(d.glob("*.txt")):
                    try:
                        messages.append({"time": f.stat().st_mtime, "message": f.read_text().strip(), "title": "local inbox"})
                    except OSError:
                        continue
        seen, out = set(), []
        for m in sorted(messages, key=lambda m: m.get("time") or 0):
            if m["message"] in seen:
                continue
            seen.add(m["message"])
            out.append(m)
        return JSONResponse({"messages": out})

    LIVE_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>StopProof live inbox</title>
<style>body{margin:0;background:#0e1014;color:#e8eaef;font:15px/1.6 system-ui,sans-serif;padding:22px}
h1{font-size:17px}.m{border-bottom:1px solid #252a33;padding:10px 0;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
.t{color:#687080;font-size:11.5px;margin-right:10px}</style>
<h1>StopProof live inbox</h1><p style="color:#9aa2b1">Every send the agent performs lands here in real time.</p>
<div id=inbox><div class=m style=color:#687080>waiting for sends…</div></div>
<script>
const t = location.pathname.split("/").pop(), box = document.getElementById("inbox");
async function poll(){ try{ const r = await fetch("/api/live/"+encodeURIComponent(t)+"/messages"); const d = await r.json();
  if(d.messages && d.messages.length){ box.innerHTML=""; for(const m of d.messages){
    const row=document.createElement("div"); row.className="m";
    const ts=document.createElement("span"); ts.className="t"; ts.textContent=new Date(m.time*1000).toLocaleTimeString();
    row.appendChild(ts); row.appendChild(document.createTextNode(m.message)); box.appendChild(row);} } }catch(e){}
  setTimeout(poll,1500);} poll();
</script>"""

    async def live_page(request: Request) -> Response:
        """Public phone-friendly inbox: open /live/<topic> anywhere and watch sends arrive."""
        topic = request.path_params["topic"]
        if not NTFY_TOPIC.match(topic):
            return JSONResponse({"error": "invalid topic"}, status_code=400)
        return HTMLResponse(LIVE_PAGE)

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
        Route("/api/live/{topic}/deliver", live_deliver, methods=["POST"]), Route("/live/{topic}", live_page),
        Route("/api/escalate", escalate, methods=["POST"]),
    ])
