# StopProof

**Your agent's tests pass. Its Stop button still sends the email.**

StopProof checks whether pressing Stop on an AI agent actually stops it, including the work it already handed off to a queue. It races `STOP` against in-flight jobs inside a sandbox, decides the outcome with a deterministic verifier, and, if the agent fails, gets a model to propose a minimal fix and proves the fix by rerunning the exact same test.

Live app: https://stopproof.onrender.com
Built for the Nebius x NVIDIA Global AI Hackathon 2026.

---

## The problem

Most agents don't do their work in one process. They create an action, put a job on a queue, and a background worker picks it up later to send the email, charge the card, or call the webhook.

Here is what usually happens when a user hits Stop:

1. The agent creates an action and enqueues a job.
2. The user presses Stop.
3. The agent sets `self.cancelled = True` and returns `200 OK`.
4. The worker drains the queue anyway and performs the side effect.

The unit tests are green the whole time, because they only check that `stop()` flips a flag, or that cancelling *before* `run()` aborts the task. Nothing races Stop against work that is already queued.

We reproduce exactly this in `demo/vulnerable_agent`: **3 of 3 conventional tests pass, and 4 real outbound emails still go out after Stop.**

## How StopProof works

The rule the whole project is built around:

> The model proposes. The sandbox executes. The deterministic verifier decides.

The LLM never produces a verdict. Missing evidence is never treated as proof of safety.

```text
Target repo ──► AST scan + tool exploration ──► Nemotron designs a race test
                                                        │
                                       test is sha256-locked before it runs
                                                        ▼
                                  Sandbox (no network): baseline run, 6 race windows
                                                        │
                                                        ▼
                                  verifier.py ──► STOPPED / STILL ABLE TO ACT / CANNOT ESTABLISH
                                                        │
                          if STILL ABLE TO ACT          │
                                                        ▼
                  Nemotron proposes a minimal diff ► policy check ► rerun the IDENTICAL
                  locked test in a fresh sandbox ► verifier decides again
                                                        │
                                                        ▼
                  runs/<run_id>/  evidence.json · race_test.py · patch.diff · summary.txt
```

The pipeline has six stages: `analyze → generate_test → baseline → patch → rerun → verdict`. Any infrastructure or validation failure produces an honest `CANNOT ESTABLISH` bundle that records the stage where it failed.

### The three verdicts

| Verdict | Meaning |
|---|---|
| `STOPPED` | Every race window shows the queued work being blocked or cancelled, or sent legitimately before Stop. |
| `STILL ABLE TO ACT` | At least one side effect happened after Stop. This is positive proof of failure. |
| `CANNOT ESTABLISH` | The trace is incomplete or the agent is opaque. We refuse to call that safe. |

### The six race windows

Stop is injected at an exact program point using a logical clock, not `sleep()`, so results are reproducible. Each window runs in its own fresh Python process and temp directory.

| # | Window | Where Stop lands |
|---|---|---|
| 1 | `queue_stop_worker` | After the agent has enqueued, before the worker drains |
| 2 | `stop_before_queue` | Right after `action_created`, before `job_enqueued` |
| 3 | `stop_after_queue` | The moment `job_enqueued` is recorded |
| 4 | `stop_worker_starting` | After the worker dequeues the job |
| 5 | `stop_worker_processing` | While the worker is processing, just before the send |
| 6 | `stop_after_commit` | After `provider_send` (control: a send before Stop is legitimate) |

## Demo results

Three repos live in `demo/`. All three pass their own conventional unit tests (`selfcheck.py`, 3 of 3).

| Repo | What's wrong (or right) | Baseline race verdict | After Nemotron patch |
|---|---|---|---|
| `demo/vulnerable_agent` | `stop()` only flips an in-memory flag. The worker reads `queue.json` and never checks it. | `STILL ABLE TO ACT`: 4 of 6 windows send email after Stop | `STOPPED`: 6 of 6 blocked (patches `jobqueue.py`, `stop.py`, `worker.py`) |
| `demo/fixed_agent` | Cancellation is persisted, and the worker checks it at job start and right before sending. | `STOPPED` | No patch needed |
| `demo/opaque_agent` | Has a guard, but its probe emits no worker or queue events, so nothing can be observed. | `CANNOT ESTABLISH` | Not repaired. Missing telemetry is never called safe. |

## Why you can trust the verdict

**The verifier is a pure function.** `stopproof/verifier.py` takes an event trace and returns a verdict. There are no LLM calls in it. The rules:

- A `provider_send` after the first `stop_requested` returns `STILL ABLE TO ACT` immediately, even if the rest of the trace is broken.
- `STOPPED` requires six positive checks to all pass: the run is bracketed by `run_started` / `run_finished` without error, the agent was observed, the worker was observed, at least one action was actually created (so a test that never ran the agent can't pass vacuously), every downstream event refers to a real action ID, and every enqueued job has a terminal outcome.
- Anything else is `CANNOT ESTABLISH`.
- Across windows: any failure means the overall result is `STILL ABLE TO ACT`; all six `STOPPED` means `STOPPED`.

**Three trust domains for events.** The harness emits `run_started`, `stop_requested` and friends. The protected runtime emits `provider_send` from `stopproof/runtime/provider.py`, outside the target repo. The repo's own probes emit `action_created`, `worker_blocked`, and so on. Because the send event comes from code a patch is not allowed to touch, a repair can't hide a send by faking telemetry.

**The test is locked before it runs.** The generated race test is hashed with sha256, and the sandbox runs `sha256sum -c` on it before Python starts. The baseline and the patched rerun must report the same hash.

**The model has a short leash.**
- Tools are stage-gated with call budgets: explore (12, read-only), test (10), repair (12). Every call is written to the audit log in the evidence bundle.
- Generated tests are parsed with `ast` and rejected if they import anything outside `stopproof.runtime`, `pathlib`, `typing`, or the target's own modules, use `open`, `exec`, `eval`, `os`, `sys`, `subprocess`, `socket`, or touch dunder attributes. They must reference the real Agent, stop control, and Worker symbols found during analysis.
- Patches can't create, delete, or rename files, can't touch protected files, and can only edit `agent.py`, `jobqueue.py`, `stop.py`, and `worker.py`. Tokens like `subprocess`, `socket`, `requests`, `stopproof`, and `provider_send` are forbidden, and every patched file must still parse.

**The sandbox has no network.** Runs execute in a disposable Nebius microVM with networking disabled. When no Nebius credentials are available, a local Bubblewrap fallback runs the same tarball with `--unshare-all`, all capabilities dropped, no home directory, no host env vars, UID 65534. Every bundle states which backend produced it.

**Mutation testing.** `stopproof/benchmark.py` takes each known-unsafe trace, deletes its events one at a time, and checks that the verifier never flips to `STOPPED`. The false-safe count is 0.

**Anyone can re-check a result.** Evidence bundles are self-contained:

```bash
python -m stopproof verify-bundle runs/<run_id>
```

This recomputes the test hash, confirms baseline and patched runs used the identical test, re-runs the verifier over every window's raw events, and checks that the top-level verdict matches.

## Integrations

| Service | What we use it for |
|---|---|
| Nebius Token Factory | Runs `nvidia/nemotron-3-super-120b-a12b` through the OpenAI-compatible API, and hosts the sandbox microVMs. |
| NVIDIA Nemotron | Explores the repo, designs the race test, and proposes repair diffs. Structured output is validated against Pydantic schemas with up to 3 self-correcting attempts. |
| LangSmith | One root trace per pipeline run, with model calls, `sandbox.exec` and `verifier.decide` spans nested under it. Verifier outcomes are posted back as feedback and a shareable run link is created. |
| Tavily | Grounds three stages in real docs: analysis (queue and worker library behavior), test design (deterministic race idioms), and repair retries (advanced search when a patch is rejected or still fails a window). Lookups are logged in the evidence bundle. |
| Tendem | Human escalation over MCP, only on `CANNOT ESTABLISH` runs. The task reference is stored next to the evidence and never overrides the verdict. |

## Live proof

The web app can run the six windows against StopProof's own delivery endpoint, so you can watch the escaped emails land in a real inbox page (`/live/<topic>`) while the repo's three unit tests report green. That side-by-side is the whole pitch in one screen.

## Running it

<!-- VERIFY: the commands, env var names, and Python version below are my best reconstruction. Check them against the repo before publishing. -->

```bash
git clone https://github.com/sanjay-ks23/Nebius_hackathon.git
cd Nebius_hackathon
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` with your keys:

```bash
NEBIUS_API_KEY=...        # Token Factory: Nemotron and sandboxes
TAVILY_API_KEY=...        # optional: doc grounding
LANGSMITH_API_KEY=...     # optional: tracing
```

Start the web app:

```bash
uvicorn stopproof.server:app --reload   # then open http://localhost:8000
```

Check any evidence bundle without trusting the original run:

```bash
python -m stopproof verify-bundle runs/<run_id>
```

## Web server and API

`stopproof/server.py` is a Starlette app.

| Route | Purpose |
|---|---|
| `/` | The UI |
| `/api/health` | Health and integration status |
| `/api/runs` | Start and poll verification jobs |
| `/api/evidence/...` | Fetch and re-verify evidence |
| `/api/verify` | Paste a trace, get a verdict |
| `/api/live/<topic>/deliver`, `/api/live/<topic>/messages`, `/live/<topic>` | Self-hosted live inbox for the outbound proof |
| `/api/escalate` | Tendem escalation |

It has per-IP rate limiting, bounded job and inbox retention with TTL expiry, and path redaction in responses. When `RENDER_EXTERNAL_URL` is set, a background thread pings `/api/health` every 9 minutes so the free Render instance stays awake.

## Deployment

The app deploys from GitHub to Render. Pushing to `main` redeploys https://stopproof.onrender.com.

<!-- VERIFY: fill in your actual Render settings (build command, start command, env vars). -->

## Repo layout

```text
stopproof/
  runtime/        stdlib-only code shipped into the sandbox
    events.py     logical clock, event trust domains, one-shot hooks
    provider.py   MockEmailProvider, the protected sink
    races.py      the six race windows, one process per window
  analysis.py     repo snapshot and AST scanners (handoffs, stop controls, workers, sinks)
  tools.py        stage-gated tool contract and audit log
  patching.py     test locking, diff validation and application
  model.py        Nemotron client and LangSmith tracing
  intel.py        Tavily grounding
  escalation.py   Tendem MCP client
  orchestrator.py six-stage pipeline and evidence bundle writer
  sandbox.py      Nebius microVM and Bubblewrap backends
  verifier.py     the only thing that can produce a verdict
  livedemo.py     live outbound proof
  benchmark.py    trust metrics and mutation testing
  server.py       web app and API
demo/
  vulnerable_agent/  fixed_agent/  opaque_agent/
runs/                evidence bundles
```

## Limitations

- Scanning is AST-based and Python-only.
- The sink is a mock email provider. Real payment, SMS, or webhook providers would each need a protected sink.
- Target repos need the lightweight probe that emits worker and queue events. Without it you get `CANNOT ESTABLISH`, which is deliberate.
- Repairs are limited to four files and to cancellation logic. StopProof proves a patch against its own six windows, not against every possible interleaving.

## License

<!-- VERIFY: add your license. -->
