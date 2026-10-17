# StopProof

**Prove your AI agent actually stopped.**

StopProof is a repository-level coding agent for *executable cancellation verification*. It analyzes an agent
codebase, finds where work leaves the agent (queue, worker, provider), generates a race test around the STOP
boundary, runs it in an isolated sandbox, and lets a **deterministic verifier** decide from machine-recorded
events whether the downstream side effect could still happen after STOP.

> Nemotron proposes -> orchestrator validates -> sandbox executes -> verifier decides.

## The problem

When a user hits STOP, killing the initiating agent does not necessarily stop work that already crossed a
boundary. In the MVP scenario ("Send Alice an email: the meeting is cancelled"):

```
agent creates job -> queue persists it -> STOP -> agent cancelled -> worker still sends -> provider records SEND
```

STOP happened before SEND, yet SEND still happened. StopProof detects that, repairs it, and re-proves it with the
*same* locked test.

## Outcomes

| Outcome | Meaning |
|---|---|
| STOPPED | Evidence establishes no protected side effect after STOP, and every action is accounted for (blocked/cancelled/never queued/completed before STOP). |
| STILL ABLE TO ACT | A `provider_send` was recorded after `stop_requested`. Always wins, even if the trace is otherwise incomplete. |
| CANNOT ESTABLISH | Missing or inconsistent evidence (no STOP boundary, opaque queue/worker, uncorrelated send, unfinished run, malformed trace, pipeline failure). Never treated as safe. |

## Architecture

```
Existing repo -> Orchestrator --(read-only tools, schema-checked proposals)--> Nemotron (Token Factory)
                     |
                     +--(bundle: harness + repo + locked test)--> Execution backend (disposable, no network)
                                                                     agent -> queue -> worker -> mock provider
                                                                     event recorder (logical clock)
                     <--------------------- traces ---------------------+
                     |
               Deterministic verifier -> STOPPED | STILL ABLE TO ACT | CANNOT ESTABLISH -> evidence bundle -> UI
```

| Path | Responsibility |
|---|---|
| `stopproof/runtime/` | Sandbox-side, stdlib-only: event recorder (`events.py`), protected mock provider (`provider.py`), race-window driver (`races.py`). |
| `stopproof/verifier.py` | The only component that produces a verdict (pure function over a trace), plus independent re-verification of stored bundles. |
| `stopproof/analysis.py` | Safe repo snapshot (no secrets/symlinks) + deterministic AST scanners for handoffs, STOP controls, workers, sinks. |
| `stopproof/tools.py` | The controlled tool contract with per-stage policy and call budgets. |
| `stopproof/patching.py` | Test validation + hash lock, patch parsing/validation/application. |
| `stopproof/agent.py`, `model.py` | Nemotron prompts, pydantic schemas, tool-use loop, validated structured output with on-disk cache. |
| `stopproof/sandbox.py` | Execution backends: Nebius Token Factory Sandbox (`contree_client`) and the local bubblewrap fallback. |
| `stopproof/orchestrator.py` | The pipeline and evidence bundle. |
| `stopproof/benchmark.py` | Trust metrics, including false-safe rate. |
| `stopproof/intel.py` | Optional Tavily grounding for analysis/repair prompts. |
| `stopproof/escalation.py` | Optional Tendem (MCP) human-review escalation for inconclusive runs. |
| `stopproof/livedemo.py` | Host-side live demo: same races, real outbound sink (e.g. ntfy.sh). |
| `stopproof/server.py`, `ui/index.html` | Demo API + single-page UI (no build step, no external assets). |
| `demo/` | `vulnerable_agent`, `fixed_agent`, and `opaque_agent` (correct but emits no worker telemetry). |

### Tool contract

`get_repo_tree`, `read_file`, `search_code`, `inspect_dependencies`, `inspect_runtime_entrypoints`, `identify_handoffs`,
`identify_stop_controls`, `generate_test`, `run_test`, `apply_patch`, `revert_patch`, `run_workflow`, `collect_events`,
`reset_environment`. Every call goes through `Tools.call`, which checks the stage (`explore`/`test`/`repair`), a call
budget, and writes an audit record (in the evidence bundle). The model can only use the read-only tools; everything
that executes code runs in an execution backend, never directly on the host.

## Setup

Requires Python 3.12+ (developed on WSL Ubuntu, Python 3.14).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in values
```

Environment variables (names only; values live in `.env`, which is gitignored):

| Variable | Purpose |
|---|---|
| `NEBIUS_API_KEY` | Token Factory inference and Sandbox auth |
| `NEBIUS_AI_PROJECT` | Token Factory project ID, sent as the Sandbox `Project` header |
| `STOPPROOF_MODEL` | Nemotron model id (default `nvidia/nemotron-3-super-120b-a12b`) |
| `STOPPROOF_SANDBOX` | Default backend: `nebius` (default) or `local` |
| `STOPPROOF_LOG_LEVEL` | Log level |
| `LANGSMITH_API_KEY`, `LANGSMITH_TRACING=true`, `LANGSMITH_PROJECT` | Optional: trace every Nemotron call (proposals stay auditable; the verifier never uses them) |
| `TAVILY_API_KEY` | Optional: ground analysis/repair prompts in real library documentation |
| `TENDEM_API_KEY` | Optional: enables "Request human expert review" on `CANNOT ESTABLISH` runs |

Start `serve` from the repository root (it lists the repositories in `demo/`).

## Execution backends

Target-repository code never runs directly on the host. Both backends receive the same deterministic tarball
(harness + repo + locked test), run it with networking off in a fresh environment, and return the same result.

| Backend | Select with | Isolation | Evidence label |
|---|---|---|---|
| Nebius Token Factory Sandbox | `--backend nebius` (default) | Disposable microVM, networking disabled | `nebius-sandbox` |
| Local isolated sandbox (fallback) | `--backend local` | bubblewrap namespaces: no network interface, read-only `/usr` only, no home directory, `/etc`, `/mnt` or environment secrets, scratch tmpfs as the only writable path, all capabilities dropped, unprivileged uid, CPU/file/memory caps, dies with its parent. **Shares the host kernel, so it is weaker than a microVM.** | `local-bubblewrap` |

The local backend exists for machines where Nebius Sandboxes is not enabled for the project. It needs `bubblewrap`
(`sudo apt install bubblewrap`) and `/usr/bin/python3`. It is never chosen silently: the CLI needs `--backend local`, the
UI preselects it only when the Nebius sandbox reports unavailable, and every evidence bundle, verdict panel and
summary states where the run executed. The UI also shows how many recorded runs were executed on the Nebius sandbox.

### If the Nebius sandbox says "not enabled"

`python -m stopproof check` prints the token's real permissions. When `spawn`, `spawn_disposable`, `import` and `list`
are all `false`, the key authenticates but the project is not allowed to start sandbox instances. Sandboxes is a Beta
feature that Nebius enables per project; StopProof cannot change it, and Nebius's own guidance for missing permissions
is that a token with different grants is required.

1. Ask Nebius to enable Sandboxes for your Token Factory project (the Beta contact in their docs is contree@nebius.com), or ask the event organisers for a sandbox-enabled key and project.
2. Put the new `NEBIUS_API_KEY` / `NEBIUS_AI_PROJECT` in `.env`.
3. Run `python -m stopproof check`, or press **Re-check** in the UI (it re-reads `.env`). The Nebius sandbox becomes the default as soon as it reports ready.

## Run

```bash
python -m stopproof check [--backend local]    # inference + every backend; runs a bundle on the selected one
python -m stopproof run demo/vulnerable_agent --backend local   # analyze -> lock test -> baseline -> patch -> same test -> verdict
python -m stopproof run demo/fixed_agent --backend local        # STOPPED at baseline, no patch needed
python -m stopproof run demo/opaque_agent --backend local       # CANNOT ESTABLISH: worker telemetry is missing
python -m stopproof serve                      # UI on http://127.0.0.1:8000
python -m stopproof verify-bundle runs/<id>    # recompute every verdict in a stored bundle
python -m stopproof verify trace.json          # verifier only, offline
python -m stopproof benchmark                  # trust metrics over runs/*/evidence.json
python -m pytest                               # full test suite (live tests skip if services are unavailable)
```

Drop `--backend local` once the Nebius sandbox is enabled. Each run writes `runs/<run_id>/` with `evidence.json`,
`summary.txt`, `race_test.py` and (if any) `patch.diff`. The first run calls Nemotron; identical repeats are served from
`.stopproof/cache` in seconds.

## Live proof (real side effect)

The UI's **Run live proof** button runs the same race windows on the host, but the demo providers perform a *real*
outbound delivery instead of only recording an event: each send POSTs to a fresh `ntfy.sh` topic, and the page streams
the messages into an in-page inbox. Open the shown `ntfy.sh/<topic>` link on a phone to watch it buzz — the broken agent
sends after STOP, the fixed agent stays silent. This is a demonstration of the same repo code and the same verifier, not
the sandboxed verification path (sandboxes run with networking disabled by design).

Every live run also writes each send to a local file inbox under `runs/live-<job>/`, and the page merges those files
into the same inbox — so proof of delivery exists even when the push service is unreachable. Sinks are comma-separated
and the demo providers understand two schemes: `https://` (POST, e.g. ntfy.sh) and `file://` (offline, one file per send).

## Human escalation (optional)

When the verifier returns `CANNOT ESTABLISH`, the UI offers **Request human expert review**: the run's verdict and the
deciding window's trace are submitted as a task through the Tendem MCP (`create_task`), and the task reference is stored
next to the evidence in `runs/<run_id>/escalation.json`. The task text is the only thing that leaves the machine, and it
never changes a recorded verdict — the human's review is supplementary evidence, not an override.

## Optional integrations

All three are additive and degrade to no-ops when their env vars are unset — they can never influence a verdict.

| Service | Where | What it does |
|---|---|---|
| LangSmith | `stopproof/model.py` | Wraps the OpenAI client; every Nemotron proposal lands as a trace tagged `stopproof.<stage>` under `LANGSMITH_PROJECT` |
| Tavily | `stopproof/intel.py` → `agent.py` | Searches real library docs during analysis, and again when a patch attempt is rejected (informed retries instead of blind ones) |
| Tendem | `stopproof/escalation.py` → `server.py` | `POST /api/escalate` hands an inconclusive run to a human expert reviewer |

## Deploying the page

The app is a long-running process (a verification run spans minutes and holds job state), so deploy it as a container,
not as serverless functions. Execution already happens in remote Nebius microVMs — the host only orchestrates — so any
host with outbound HTTPS works.

```bash
docker build -t stopproof .
docker run --env-file .env -p 8000:8000 stopproof     # http://localhost:8000
```

Or for an instant public URL during a demo, from the machine already running `serve`:

```bash
cloudflared tunnel --url http://localhost:8000        # prints a public https URL
```

Deployment notes: set `STOPPROOF_SANDBOX=nebius` (bubblewrap needs kernel namespaces most container hosts deny), pass
secrets via env (`--env-file`), never bake `.env` into an image — `.dockerignore` already excludes it. Evidence is written
to `runs/` inside the container (mount a volume to keep it across redeploys).

## Manual verification guide

This walks through verifying StopProof's claims yourself, using the web page and then without it.

### 1. Start the page

```bash
python -m stopproof serve        # from the repository root
```

Open http://127.0.0.1:8000. The header shows three services. **Nemotron** must be ready. **Nebius sandbox** is green
only if Nebius enabled Sandboxes for your project; otherwise **Local sandbox** must be ready and is preselected. A run
URL such as `/#run-1234abcd` opens a recorded run directly.

### 2. Reproduce the failure and the repair

1. Repository `vulnerable_agent`, press **Run verification**. The Progress list shows each stage; a first run takes
   a few minutes because Nemotron analyzes, writes the test and proposes patches.
2. The verdict is the final state. On the **Baseline** tab it must be **STILL ABLE TO ACT**. In the Timeline find the
   red **STOP requested** row; the column *vs STOP* is the distance in logical time from STOP. Rows after it carry an
   amber bar, and the **Provider SEND** row is flagged *late side effect* with a positive offset. That row is the
   proof: the mock provider recorded the send after STOP, although the agent had been cancelled.
3. Switch to **After patch (same locked test)**. Every window must now be STOPPED; the queued job reads *cancelled* or
   the worker reads *blocked by cancellation guard*, and there is no Provider SEND after STOP.
4. Open the other windows (or click a row of the **Race windows** table). Six interleavings run against the same test.
   For `vulnerable_agent`, `stop_before_queue` and `stop_after_commit` are STOPPED even at baseline (the email was never
   queued, or was already sent before STOP); the other four reproduce the late effect.

### 3. Check that nothing was rigged

| Check | Where | Expected |
|---|---|---|
| The test was not changed after the baseline | **Locked test**: *Baseline ran* / *After-patch run* | both `identical`, same sha256 |
| The hash was enforced where the code ran | **Locked test**: *Hash checked in sandbox* | `sha256sum -c before each run` |
| The patch is narrow | **Patch**, **What StopProof found** | only *patchable* files changed; *protected* files (`probe.py`, `provider.py`) untouched; rejected attempts listed with reasons |
| The verdict is not the model's opinion | verdict footer | *Decided by deterministic verifier over recorded events* |
| Where it ran | verdict footer, *Executed in* | `Nebius Token Factory Sandbox` or `Local bubblewrap sandbox (not the Nebius sandbox)` |
| Recorded verdicts are consistent | verdict footer *Integrity checks* | `18 of 18 pass (recomputed)` |

### 4. Recompute the verdicts yourself

* In the page: **Evidence -> Re-run integrity checks** recomputes every window verdict from the raw events, re-aggregates
  the outcomes, recomputes the locked-test hash and confirms both runs used it.
* In a terminal, without the page: `python -m stopproof verify-bundle runs/<run_id>` prints PASS/FAIL per check.
* Download `evidence.json` from the page and edit it (flip a recorded verdict, delete an event, edit the test source):
  `verify-bundle` fails, which is how tampering shows up.

### 5. Try to break the verifier

Open **Edit this trace and re-verify** under the Timeline. The text area holds the exact events of the selected window;
nothing is sent to a model.

| Edit | Expected verdict |
|---|---|
| nothing | same as the window |
| STOPPED window: delete the `worker_blocked` / `job_cancelled` event | **CANNOT ESTABLISH** (no observation that the queued job ended) |
| STOPPED window: delete `stop_requested` | **CANNOT ESTABLISH** (STOP boundary never observed) |
| STOPPED window: add `{"event":"provider_send","ts":<later than STOP>,"action_id":"email-1"}` | **STILL ABLE TO ACT** |
| malformed JSON or an unknown event | an error / **CANNOT ESTABLISH** |

It must never answer STOPPED when evidence is missing; the same property is checked by the mutation test (every
single-event deletion of every known-unsafe trace is fed to the verifier).

### 6. The other two repositories

| Repository | Baseline | After patch |
|---|---|---|
| `vulnerable_agent` | STILL ABLE TO ACT | STOPPED |
| `fixed_agent` | STOPPED | no patch needed |
| `opaque_agent` | CANNOT ESTABLISH ("no worker/queue observation for queued job") | not repaired |

`opaque_agent`'s guard may well work, but its worker emits no events, so safety cannot be shown, and StopProof says so
instead of guessing.

### 7. Read the trust metrics

The **Trust metrics** panel is computed from the recorded bundles: the false-safe rate (STOPPED reports that were
wrong; the target is 0), known failures reproduced, handoffs identified, patch success, reproducibility, mean
verification time, and how many runs executed on the Nebius sandbox. Bundles recorded by development test doubles or
before backends were labelled appear as `legacy` / `test double` in the **Recorded runs** list.

### Troubleshooting

| Message | Meaning / fix |
|---|---|
| `Nebius sandbox: not enabled` | see *If the Nebius sandbox says "not enabled"* above; use the local sandbox meanwhile |
| `Local sandbox: not installed` | `sudo apt install bubblewrap`; on WSL2 unprivileged user namespaces must be enabled (default) |
| `Nemotron: unavailable` | `NEBIUS_API_KEY` missing or invalid; run `python -m stopproof check` |
| Repository list is empty | start `serve` from the repository root (it reads `demo/`) |
| Changed `.env` while the server runs | press **Re-check** |
| Port busy | `python -m stopproof serve --port 8001` |

## How the verifier works

Input: a trace of `{event, ts, action_id?}` records with logical timestamps (1000, 1100, ...). Rules:

1. Malformed trace (unknown event, non-monotonic ts) -> CANNOT ESTABLISH.
2. No `stop_requested` -> CANNOT ESTABLISH.
3. Any `provider_send` with `ts > stop.ts` -> **STILL ABLE TO ACT**.
4. Otherwise STOPPED only if all checks pass: run bracketed without error, agent and worker observed to finish, at
   least one action created, action identities consistent, and every queued job has an observed disposition
   (sent before STOP, `worker_blocked`, or `job_cancelled`). Otherwise CANNOT ESTABLISH.

`provider_send` is emitted by the protected mock provider; `stop_requested` and the run brackets are emitted by the
harness. Repo-side probes (`worker_blocked`, ...) can only *add* positive evidence, and a patch cannot hide a real send.

## Race windows

Reproduced deterministically, one fresh interpreter per window, on one thread, with STOP injected synchronously at a
named event (no sleeps):

1. `queue_stop_worker`: queue first, STOP second, worker third (primary)
2. `stop_before_queue`: STOP after the action is created, before it is queued
3. `stop_after_queue`: STOP immediately after queueing
4. `stop_worker_starting`: STOP as the worker starts the job
5. `stop_worker_processing`: STOP while the worker is processing, before the send
6. `stop_after_commit`: STOP after the send already happened

Reproduce a window set outside the pipeline (developing StopProof only, never the verification path):
`cd demo/vulnerable_agent && PYTHONPATH=../..:. python ../../tests/reference_race.py`.

## Security model

- **Execution**: all target code runs in a disposable backend with networking disabled (see *Execution backends*),
  fed by one deterministic tarball (harness + repo + locked test). No state survives a run, which is the reset
  mechanism; each run also gets a fresh `run_id`, queue, provider and interpreter per window.
- **Locked test**: the generated test is validated statically (imports, forbidden names, must exercise the analysed
  agent/STOP/worker), hashed, and frozen once the baseline has run. The hash is re-checked by the orchestrator and by
  `sha256sum -c` inside the backend before every run. The race schedule lives in the harness, not in the test.
- **Patches**: model edits (plain-text SEARCH/REPLACE blocks, parsed strictly; a reply without valid blocks is rejected) are rendered as a unified diff and applied as a text transform only if they touch allowed
  files (handoff/stop/worker logic), never protected ones (sink adapter, probe shim, anything importing the StopProof
  runtime), create/delete nothing, add no process/network/eval constructs, still parse, and stay small. Repair success
  is only ever declared by re-executing the locked test and re-running the verifier.
- **Secrets**: `.env` and key-like files are never read into snapshots or prompts; keys are never logged or written to
  evidence; the local sandbox starts with an empty environment.
- **UI/API**: the server binds to 127.0.0.1; the trace editor only runs the deterministic verifier.

## Limitations

- Python repositories with an in-process/file-backed queue and a Python worker; one action boundary (Agent -> Queue -> Worker -> mock email provider).
- The threat model is buggy/incomplete cancellation, not an adversarial repo actively subverting the harness inside the sandbox.
- Repo probes (`emit(...)`) are required for positive evidence; without them the answer is CANNOT ESTABLISH by design.
- The scenario is single-threaded and logical-clock based: it reproduces the ordering, not real scheduler nondeterminism.
- The local fallback shares the host kernel (namespaces, not a VM). Its results are valid evidence of the ordering, but are labelled as not Nebius-sandbox-verified.
- Patch application is done by the orchestrator as a pure text transform and the patched files are shipped into a fresh sandbox (rather than patching inside a persistent sandbox).
- Generated tests and patches depend on the model; the loop allows 3 test attempts and 4 repair attempts and fails clearly (CANNOT ESTABLISH) if none is valid.
- The Nebius sandbox path is covered by contract tests against the SDK's in-memory client, but could not be exercised live without a project that has Sandboxes enabled.

## Out of scope

Real email delivery, third-party integrations, IAM/OAuth, multi-agent analysis, formal verification of arbitrary
distributed systems, production deployment.
