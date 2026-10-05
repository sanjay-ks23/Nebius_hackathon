"""Command line entry point: python -m stopproof <command>."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .sandbox import BACKENDS


def _setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("STOPPROOF_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "httpcore", "openai"):  # request logs from these are noise, never needed
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _default_backend() -> str:
    return os.environ.get("STOPPROOF_SANDBOX", "nebius")


NOT_ENABLED_HINT = (
    "hint: Sandboxes is not enabled for this Nebius project. Ask Nebius to enable it (Beta: contree@nebius.com), or rerun with "
    "`--backend local` to use the isolated local fallback; its evidence is labelled 'local-bubblewrap', not Nebius-sandbox-verified."
)


def cmd_check(args: argparse.Namespace) -> int:
    from .model import Nemotron
    from .sandbox import backend_status, make_sandbox

    status = 0
    try:
        model = Nemotron(cache=False)
        reply = model.client.chat.completions.create(model=model.model, messages=[{"role": "user", "content": 'Reply with the JSON {"ok": true}'}], max_tokens=200, response_format={"type": "json_object"})
        print(f"[ok]   Token Factory inference: model={model.model} reply={reply.choices[0].message.content.strip()[:40]}")
    except Exception as exc:
        print(f"[fail] Token Factory inference: {type(exc).__name__}: {str(exc)[:200]}")
        status = 1
    for name, info in backend_status().items():
        print(f"[{'ok' if info['ok'] else 'fail'}]   backend '{name}' - {info['label']}: {info['detail'][:230]}")
    try:
        result = make_sandbox(args.backend).check()
        print(f"[ok]   selected backend '{args.backend}' executed a bundle: op={result['operation_id'][:8]} exit={result['exit_code']} output={result['stdout'].strip().splitlines()[-1]!r}")
    except Exception as exc:
        print(f"[fail] selected backend '{args.backend}': {type(exc).__name__}: {str(exc)[:300]}")
        if getattr(exc, "code", None) == "not_enabled":
            print(NOT_ENABLED_HINT)
        status = 1
    return status


def cmd_run(args: argparse.Namespace) -> int:
    from .model import ModelError, Nemotron
    from .orchestrator import Pipeline
    from .sandbox import SandboxUnavailable, make_sandbox

    try:
        runner = make_sandbox(args.backend)
        pipeline = Pipeline(Path(args.repo), Nemotron(), runner, Path(args.out))
    except (SandboxUnavailable, ModelError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        if getattr(exc, "code", None) == "not_enabled":
            print(NOT_ENABLED_HINT, file=sys.stderr)
        return 2
    evidence = pipeline.run()
    print(evidence["summary"])
    print(f"evidence bundle: {evidence['evidence_path']}")
    return 2 if evidence.get("error") else 0


def cmd_verify(args: argparse.Namespace) -> int:
    from .verifier import verify

    path = Path(args.trace)
    trace = json.loads(path.read_text())
    if isinstance(trace, dict) and "events" in trace:
        trace.setdefault("run_id", path.stem)  # bare {"events": [...]} files are accepted
    print(json.dumps(verify(trace).to_dict(), indent=2))
    return 0


def cmd_verify_bundle(args: argparse.Namespace) -> int:
    from .verifier import reverify_bundle

    path = Path(args.bundle)
    path = path / "evidence.json" if path.is_dir() else path
    report = reverify_bundle(json.loads(path.read_text()))
    for check in report["checks"]:
        print(f"{'PASS' if check['passed'] else 'FAIL'}  {check['name']}" + (f"  [{check['detail']}]" if not check["passed"] and check["detail"] else ""))
    print(f"{report['passed']}/{report['total']} checks passed")
    return 0 if report["ok"] else 1


def cmd_benchmark(args: argparse.Namespace) -> int:
    from .benchmark import load_evidence, score

    print(json.dumps(score(load_evidence(Path(args.runs))), indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .server import create_app

    uvicorn.run(create_app(Path(args.runs)), host=args.host, port=args.port, log_level="warning")
    return 0


def main() -> None:
    load_dotenv()
    _setup_logging()
    parser = argparse.ArgumentParser(prog="stopproof", description="Prove your AI agent actually stopped.")
    sub = parser.add_subparsers(dest="command", required=True)
    backend_help = "execution backend: 'nebius' (Token Factory Sandbox, default) or 'local' (isolated local fallback)"
    check = sub.add_parser("check", help="verify Token Factory inference and execution backends")
    check.add_argument("--backend", choices=sorted(BACKENDS), default=_default_backend(), help=backend_help)
    check.set_defaults(fn=cmd_check)
    run = sub.add_parser("run", help="analyze a repository, reproduce late effects, repair, and verify")
    run.add_argument("repo")
    run.add_argument("--out", default="runs")
    run.add_argument("--backend", choices=sorted(BACKENDS), default=_default_backend(), help=backend_help)
    run.set_defaults(fn=cmd_run)
    ver = sub.add_parser("verify", help="run the deterministic verifier over a trace JSON file")
    ver.add_argument("trace")
    ver.set_defaults(fn=cmd_verify)
    vb = sub.add_parser("verify-bundle", help="independently recompute every verdict in a stored evidence bundle")
    vb.add_argument("bundle", help="path to evidence.json or its run directory")
    vb.set_defaults(fn=cmd_verify_bundle)
    bench = sub.add_parser("benchmark", help="trust metrics over stored evidence bundles")
    bench.add_argument("--runs", default="runs")
    bench.set_defaults(fn=cmd_benchmark)
    serve = sub.add_parser("serve", help="start the demo UI")
    serve.add_argument("--runs", default="runs")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(fn=cmd_serve)
    args = parser.parse_args()
    sys.exit(args.fn(args))
