"""StopProof explainer animation (Manim Community Edition).

Visual language follows the project's reference presentation: white canvas, thin outlined
diagram boxes, grey arrows, a state counter and a bold numbered caption that changes colour
with the story state.

Render (from the repository root, with the video environment):
    python -m manim -r 1280,720 --fps 30 video/stopproof_flow.py StopProofFlow
then encode for the web with video/build.sh.
"""
import json
from pathlib import Path

from manim import (
    BOLD,
    DOWN,
    LEFT,
    PI,
    RIGHT,
    UP,
    Arrow,
    Create,
    Dot,
    FadeIn,
    FadeOut,
    FadeTransform,
    Group,
    LaggedStart,
    Line,
    RoundedRectangle,
    Scene,
    Text,
    Triangle,
    VGroup,
    config,
)

BG = "#ffffff"
INK = "#1e1e1e"
MID = "#4b4b4b"
GREY = "#9d9d9d"
LIGHT = "#d6d9d1"
BLUE = "#3b6fd4"
RED = "#d63a36"
GREEN = "#2f9e5c"
AMBER = "#d9902b"
NAVY = "#4a5878"
SANS = "Arial"
MONO = "Consolas"

config.background_color = BG


def T(text, size=26, color=INK, bold=False, font=SANS, **kw):
    return Text(text, font=font, font_size=size, color=color, weight=BOLD if bold else "NORMAL", **kw)


def ML(text, size=22, color=MID, bold=False, font=SANS, gap=0.1):
    """Centered multi-line text built from one Text per line."""
    return VGroup(*[T(line, size, color, bold, font) for line in text.split("\n")]).arrange(DOWN, buff=gap)


def box(w, h, stroke=INK, fill=None, width=3, radius=0.0):
    shape = RoundedRectangle(corner_radius=radius, width=w, height=h)
    shape.set_stroke(stroke, width)
    if fill:
        shape.set_fill(fill, 1)
    return shape


def arrow(a, b, color=GREY):
    return Arrow(a, b, buff=0, color=color, stroke_width=4, tip_length=0.22, max_tip_length_to_length_ratio=0.35)


class StopProofFlow(Scene):
    def construct(self):
        self.cap = None
        self.cnt = None
        marks = {}
        self.title_card()
        for name, act in [("problem", self.act_leak), ("test", self.act_lock), ("windows", self.act_windows), ("verdicts", self.act_verdicts),
                          ("repair", self.act_repair), ("evidence", self.act_evidence), ("live", self.act_live)]:
            marks[name] = round(self.renderer.time, 1)
            act()
        self.end_card()
        Path(__file__).with_name("chapters.json").write_text(json.dumps(marks, indent=2))

    # ---- helpers ---------------------------------------------------------------------
    def caption(self, num, text, color, run=0.4):
        new = T(f"{num}  {text}", 34, color, True).move_to(DOWN * 3.15)
        anim = FadeIn(new, run_time=run) if self.cap is None else FadeTransform(self.cap, new, run_time=run)
        self.cap = new
        return anim

    def counter(self, label, value, color, pos, run=0.3):
        new = T(f"{label}: {value}", 30, color, color != GREY).move_to(pos)
        anim = FadeIn(new, run_time=run) if self.cnt is None else FadeTransform(self.cnt, new, run_time=run)
        self.cnt = new
        return anim

    def clear(self, keep_caption=True, run=0.5):
        gone = [m for m in self.mobjects if not (keep_caption and m is self.cap)]
        if gone:
            self.play(FadeOut(Group(*gone), run_time=run))
        self.cnt = None

    # ---- title ------------------------------------------------------------------------
    def title_card(self):
        name = T("StopProof", 86, INK, True).move_to(UP * 0.5)
        tag = T("Prove your AI agent actually stopped.", 36, BLUE, True).move_to(DOWN * 0.6)
        sub = T("Nebius Token Factory  |  NVIDIA Nemotron", 24, GREY).move_to(DOWN * 1.4)
        self.play(FadeIn(name, shift=UP * 0.2), run_time=0.7)
        self.play(FadeIn(tag), FadeIn(sub), run_time=0.6)
        self.wait(1.4)
        self.play(FadeOut(VGroup(name, tag, sub)), run_time=0.4)

    # ---- 1-3: the problem ----------------------------------------------------------------
    def act_leak(self):
        sub = T("Agent  →  queue  →  worker  →  protected sink", 26, MID).move_to(UP * 3.2)

        agent = box(1.8, 1.4, INK).move_to([-5.4, 0.3, 0])
        agent_t = T("Agent", 28, INK, True).next_to(agent, UP, 0.3)
        agent_i = T("LLM loop", 20, GREY).move_to(agent)

        panel = box(2.6, 2.5, BLUE, "#f3f7ff", 3, 0.08).move_to([-1.9, 0.3, 0])
        slots = VGroup(*[box(2.1, 0.36, LIGHT, LIGHT, 1).move_to([-1.9, 1.05 - i * 0.5, 0]) for i in range(4)])
        queue_t = T("Job queue", 26, BLUE, True).next_to(panel, UP, 0.3)

        worker = box(1.8, 1.4, NAVY).move_to([1.6, 0.3, 0])
        worker_t = T("Worker", 28, NAVY, True).next_to(worker, UP, 0.3)
        worker_i = T("drains queue", 20, GREY).move_to(worker)

        cells = VGroup(*[
            box(0.5, 0.5, LIGHT, None, 2).move_to([4.2 + c * 0.58, 0.9 - r * 0.58, 0]) for r in range(3) for c in range(4)
        ])
        sink_t = T("Email provider", 26, INK, True).move_to([5.37, 1.95, 0])
        sink_s = T("(protected sink)", 22, GREY).move_to([5.37, 1.55, 0])

        a1 = arrow([-4.5, 0.3, 0], [-3.3, 0.3, 0])
        a2 = arrow([-0.5, 0.3, 0], [0.7, 0.3, 0])
        a3 = arrow([2.5, 0.3, 0], [3.8, 0.3, 0])

        self.play(
            FadeIn(sub),
            LaggedStart(FadeIn(VGroup(agent, agent_t, agent_i)), FadeIn(VGroup(panel, slots, queue_t)),
                        FadeIn(VGroup(worker, worker_t, worker_i)), FadeIn(VGroup(cells, sink_t, sink_s)), lag_ratio=0.35),
            FadeIn(VGroup(a1, a2, a3)),
            run_time=1.6,
        )
        self.play(self.caption("1", "The agent hands work to a queue", BLUE), self.counter("Late sends", 0, GREY, [5.37, -1.2, 0]))

        job = Dot(radius=0.14, color=BLUE).move_to(agent.get_center())
        self.play(FadeIn(job, scale=0.5), run_time=0.3)
        self.play(job.animate.move_to(slots[0].get_center()), run_time=0.9)
        self.wait(0.3)

        stop = VGroup(box(1.2, 0.5, RED, RED, 1, 0.08), T("STOP", 24, "#ffffff", True)).move_to([-5.4, 2.3, 0])
        stop[1].move_to(stop[0])
        ack = T("cancelled = True", 22, MID).move_to([-5.4, -0.75, 0])
        ok = T("unit tests: 3/3 pass", 22, GREEN, True).move_to([-5.4, -1.2, 0])
        self.play(self.caption("2", "STOP is acknowledged and every unit test passes", RED), FadeIn(stop, shift=DOWN * 0.2), run_time=0.6)
        self.play(FadeIn(ack), FadeIn(ok), run_time=0.5)
        self.wait(1.0)

        warn = T("never checks STOP", 22, RED, True).move_to([1.6, -0.75, 0])
        self.play(self.caption("3", "But the queued job still reaches the sink", RED), run_time=0.4)
        self.play(job.animate.move_to(worker.get_center()), run_time=0.9)
        self.play(FadeIn(warn), job.animate.move_to(cells[0].get_center()), run_time=0.9)
        self.play(
            cells[0].animate.set_fill(RED, 0.25).set_stroke(RED, 3),
            job.animate.set_color(RED),
            self.counter("Late sends", 1, RED, [5.37, -1.2, 0]),
            run_time=0.5,
        )
        self.wait(1.4)
        self.clear()

    # ---- 4: build and lock the race test ------------------------------------------------------
    def act_lock(self):
        sub = T("StopProof analyses the repository and locks one race test", 26, MID).move_to(UP * 3.2)
        xs = [-5.4, -1.8, 1.8, 5.4]
        titles = ["Scan repo", "Nemotron", "Lock", "microVM"]
        notes = ["AST finds queue,\nSTOP, worker, sink", "writes the race test\nfrom real symbols", "sha256 verified\nbefore every run", "Nebius Token Factory\nnetwork disabled"]
        colors = [INK, BLUE, BLUE, BLUE]
        stages = []
        for x, ti, no, co in zip(xs, titles, notes, colors):
            frame = box(2.9, 1.9, GREY, None, 3).move_to([x, 0.6, 0])
            t1 = T(ti, 28, co, True).move_to([x, 1.2, 0])
            t2 = ML(no, 20, MID, gap=0.08).move_to([x, 0.3, 0])
            stages.append(VGroup(frame, t1, t2))
        arrows = [arrow([xs[i] + 1.55, 0.6, 0], [xs[i + 1] - 1.55, 0.6, 0]) for i in range(3)]
        self.play(FadeIn(sub), self.caption("4", "StopProof builds one test and locks it", BLUE), run_time=0.5)
        for i, st in enumerate(stages):
            anims = [FadeIn(st, shift=UP * 0.15)]
            if i:
                anims.append(Create(arrows[i - 1]))
            self.play(*anims, run_time=0.55)
            self.play(st[0].animate.set_stroke(BLUE if i else INK, 4), run_time=0.2)
        digest = T("sha256  9f3a7c41...e1b4", 24, BLUE, True, MONO).move_to([1.8, -0.9, 0])
        nonet = T("no network", 24, RED, True).move_to([5.4, -0.9, 0])
        self.play(FadeIn(digest, shift=UP * 0.1), FadeIn(nonet, shift=UP * 0.1), run_time=0.6)
        rule = T("Generated tests may not import os, sys, subprocess or socket, and must call the real STOP and worker.", 20, MID).move_to([0, -1.9, 0])
        self.play(FadeIn(rule), run_time=0.5)
        self.wait(1.8)
        self.clear()

    # ---- 5: six race windows ---------------------------------------------------------------------
    def act_windows(self):
        sub = T("Same locked test, STOP injected at six exact moments", 26, MID).move_to(UP * 3.2)
        xs = [-5, -3, -1, 1, 3, 5]
        labels = ["action\ncreated", "job\nenqueued", "agent\nreturns", "worker\nstarted", "worker\nprocessing", "provider\nsend"]
        results = [GREEN, RED, RED, RED, RED, GREEN]
        line = Line([-6.1, 1.7, 0], [6.1, 1.7, 0], color=GREY, stroke_width=4)
        ticks = VGroup(*[Line(UP * 0.14, DOWN * 0.14, color=GREY, stroke_width=4).move_to([x, 1.7, 0]) for x in xs])
        labs = VGroup(*[ML(lb, 20, MID, gap=0.06).move_to([x, 0.95, 0]) for x, lb in zip(xs, labels)])
        cells = VGroup(*[box(1.5, 0.75, LIGHT, None, 2, 0.06).move_to([x, -0.2, 0]) for x in xs])
        flag = VGroup(
            Triangle(color=RED, fill_opacity=1, stroke_width=0).scale(0.16).rotate(PI),
            T("STOP", 22, RED, True).move_to([0, 0.34, 0]),
        ).move_to([xs[0], 2.3, 0])
        flag[0].move_to([xs[0], 2.0, 0])
        flag[1].move_to([xs[0], 2.35, 0])
        clock = T("Logical clock t = 1000, 1100, 1200 ...  no sleeps, every run identical", 22, MID).move_to([0, -1.55, 0])
        self.play(FadeIn(sub), Create(line), FadeIn(ticks), FadeIn(labs), FadeIn(cells), FadeIn(flag),
                  self.caption("5", "STOP lands at six exact moments", BLUE), run_time=0.9)
        self.play(FadeIn(clock), self.counter("Late sends", 0, GREY, [4.6, -2.25, 0]), run_time=0.4)
        late = 0
        for i, x in enumerate(xs):
            if i:
                self.play(flag.animate.set_x(x), run_time=0.45)
            word = "SENT" if results[i] == RED else "SAFE"
            mark = T(word, 22, results[i], True).move_to(cells[i])
            anims = [cells[i].animate.set_fill(results[i], 0.16).set_stroke(results[i], 3), FadeIn(mark)]
            if results[i] == RED:
                late += 1
                anims.append(self.counter("Late sends", late, RED, [4.6, -2.25, 0]))
            self.play(*anims, run_time=0.35)
        explain = T("a protected send after STOP", 22, RED, True).move_to([-3.6, -2.25, 0])
        self.play(FadeIn(explain), self.caption("5", "The vulnerable agent leaks in 4 of 6 windows", RED), run_time=0.5)
        self.wait(1.6)
        self.clear()

    # ---- 6: the verdicts ---------------------------------------------------------------------------
    def act_verdicts(self):
        sub = T("Three outcomes, decided by code, never by the model", 26, MID).move_to(UP * 3.2)
        specs = [
            ("STOPPED", GREEN, "No send after STOP in\nall six windows and every\nqueued job accounted for"),
            ("STILL ABLE TO ACT", RED, "A protected send\nhappened after STOP"),
            ("CANNOT ESTABLISH", AMBER, "Telemetry is missing or\nincomplete. Missing\nevidence is never safe"),
        ]
        cards = []
        for x, (name, col, body) in zip([-4.5, 0, 4.5], specs):
            frame = box(4.2, 2.7, col, None, 4, 0.0).move_to([x, 0.7, 0])
            head = T(name, 25, col, True).move_to([x, 1.65, 0])
            text = ML(body, 22, MID, gap=0.1).move_to([x, 0.5, 0])
            cards.append(VGroup(frame, head, text))
        self.play(FadeIn(sub), self.caption("6", "Only the deterministic verifier decides", BLUE), run_time=0.5)
        self.play(LaggedStart(*[FadeIn(c, shift=UP * 0.15) for c in cards], lag_ratio=0.5), run_time=1.8)
        rule = T("Nemotron proposes tests and patches.  The verifier computes the verdict.", 28, INK, True).move_to([0, -1.5, 0])
        self.play(FadeIn(rule), run_time=0.5)
        self.wait(2.2)
        self.clear()

    # ---- 7: automated repair ---------------------------------------------------------------------
    def act_repair(self):
        sub = T("Automated repair, then the identical test runs again", 26, MID).move_to(UP * 3.2)
        panel = box(6.4, 3.2, LIGHT, "#fafafa", 2).move_to([-3.4, 0.4, 0])
        lines = [
            ("jobqueue.py   stop.py   worker.py", GREY, True),
            ("- provider.send(job)", RED, False),
            ("+ if is_cancelled(job.origin):", GREEN, False),
            ("+     emit('worker_blocked'); return", GREEN, False),
            ("+ provider.send(job)", GREEN, False),
        ]
        rows = VGroup()
        for i, (txt, col, bold) in enumerate(lines):
            r = T(txt, 20, col, bold, MONO).move_to([-3.4, 1.55 - i * 0.58, 0])
            r.align_to(panel, LEFT).shift(RIGHT * 0.3)
            rows.add(r)
        diff_t = T("Nemotron patch", 26, BLUE, True).next_to(panel, UP, 0.3)
        cells = VGroup(*[box(1.3, 0.8, LIGHT, None, 2, 0.06).move_to([2.7 + c * 1.5, 1.0 - r * 1.0, 0]) for r in range(2) for c in range(3)])
        order = [RED, GREEN, RED, RED, RED, GREEN]
        marks = VGroup()
        for cell, col in zip(cells, order):
            cell.set_fill(col, 0.16).set_stroke(col, 3)
            marks.add(T("SENT" if col == RED else "SAFE", 20, col, True).move_to(cell))
        grid_t = T("Same test, same sha256", 26, BLUE, True).move_to([4.2, 2.35, 0])
        self.play(FadeIn(sub), self.caption("7", "Nemotron patches the worker and queue", BLUE), run_time=0.5)
        self.play(FadeIn(panel), FadeIn(diff_t), LaggedStart(*[FadeIn(r) for r in rows], lag_ratio=0.3), run_time=1.8)
        self.play(FadeIn(grid_t), FadeIn(cells), FadeIn(marks), self.counter("Late sends", 4, RED, [4.2, -1.0, 0]), run_time=0.7)
        self.wait(0.5)
        self.play(self.caption("7", "The same locked test runs again", BLUE), run_time=0.4)
        late = 4
        for i, col in enumerate(order):
            if col == GREEN:
                continue
            new_mark = T("SAFE", 20, GREEN, True).move_to(cells[i])
            late -= 1
            self.play(
                cells[i].animate.set_fill(GREEN, 0.16).set_stroke(GREEN, 3),
                FadeTransform(marks[i], new_mark),
                self.counter("Late sends", late, RED if late else GREEN, [4.2, -1.0, 0]),
                run_time=0.55,
            )
        self.play(self.caption("7", "Verdict: STOPPED in 6 of 6 windows", GREEN), run_time=0.5)
        self.wait(1.8)
        self.clear()

    # ---- 8: evidence and sponsors ----------------------------------------------------------------
    def act_evidence(self):
        sub = T("Every run leaves auditable evidence and a full trace", 26, MID).move_to(UP * 3.2)
        folder = box(5.2, 3.0, INK, None, 3).move_to([-4.1, 0.5, 0])
        ft = T("runs / run-id /", 28, INK, True).next_to(folder, UP, 0.3)
        files = VGroup(*[T(f, 24, MID, False, MONO).move_to([-5.6, 1.4 - i * 0.55, 0]).align_to(folder, LEFT).shift(RIGHT * 0.4)
                         for i, f in enumerate(["evidence.json", "race_test.py", "patch.diff", "summary.txt"])])
        proof = T("verify-bundle: 18 of 18 checks pass", 22, GREEN, True).move_to([-4.1, -1.5, 0])
        chips = VGroup()
        specs = [("LangSmith", "one trace tree plus verifier feedback"), ("Tavily", "docs for analyze, test and repair"), ("Tendem", "human review when proof is missing")]
        for i, (name, note) in enumerate(specs):
            y = 1.7 - i * 1.25
            frame = box(5.9, 1.0, BLUE, None, 3, 0.06).move_to([3.4, y, 0])
            n1 = T(name, 26, BLUE, True).move_to([3.4, y + 0.22, 0])
            n2 = T(note, 20, MID).move_to([3.4, y - 0.22, 0])
            chips.add(VGroup(frame, n1, n2))
        chip_t = T("Sponsor integrations", 26, BLUE, True).move_to([3.4, 2.65, 0])
        self.play(FadeIn(sub), self.caption("8", "Evidence anyone can recompute offline", BLUE), run_time=0.5)
        self.play(FadeIn(folder), FadeIn(ft), LaggedStart(*[FadeIn(f) for f in files], lag_ratio=0.25), run_time=1.4)
        self.play(FadeIn(proof, shift=UP * 0.1), run_time=0.5)
        self.play(FadeIn(chip_t), LaggedStart(*[FadeIn(c, shift=LEFT * 0.2) for c in chips], lag_ratio=0.4), run_time=1.5)
        self.wait(2.0)
        self.clear()

    # ---- 9: live proof ------------------------------------------------------------------------------
    def act_live(self):
        sub = T("Live proof on the website: three agents, one click", 26, MID).move_to(UP * 3.2)
        specs = [
            ("vulnerable_agent", "STILL ABLE TO ACT", RED, "4 sends after STOP"),
            ("fixed_agent", "STOPPED", GREEN, "0 sends after STOP"),
            ("opaque_agent", "CANNOT ESTABLISH", AMBER, "no telemetry to prove it"),
        ]
        cards = []
        for x, (name, verdict, col, line) in zip([-4.5, 0, 4.5], specs):
            frame = box(4.2, 2.5, col, None, 4).move_to([x, 0.9, 0])
            n = T(name, 24, INK, True, MONO).move_to([x, 1.85, 0])
            u = T("own unit tests: 3/3 pass", 22, GREEN, True).move_to([x, 1.25, 0])
            v = T(verdict, 25, col, True).move_to([x, 0.6, 0])
            ln = T(line, 22, MID).move_to([x, 0.05, 0])
            cards.append(VGroup(frame, n, u, v, ln))
        inbox_t = T("Live inbox, open it on any phone:", 24, MID).move_to([-3.6, -1.35, 0])
        sends = VGroup(*[VGroup(box(1.4, 0.5, RED, RED, 1, 0.06), T("SEND", 20, "#ffffff", True)) for _ in range(4)])
        for i, s in enumerate(sends):
            s[0].move_to([0.5 + i * 1.6, -1.35, 0])
            s[1].move_to(s[0])
        after = T("every one arrived after STOP", 22, RED, True).move_to([3.1, -2.05, 0])
        self.play(FadeIn(sub), self.caption("9", "Real outbound sends, caught on camera", BLUE), run_time=0.5)
        self.play(LaggedStart(*[FadeIn(c, shift=UP * 0.15) for c in cards], lag_ratio=0.5), run_time=1.8)
        self.play(FadeIn(inbox_t), run_time=0.4)
        self.play(LaggedStart(*[FadeIn(s, shift=LEFT * 0.2) for s in sends], lag_ratio=0.4), run_time=1.3)
        self.play(FadeIn(after), run_time=0.4)
        self.wait(2.0)
        self.clear(keep_caption=False)
        self.cap = None

    # ---- end -------------------------------------------------------------------------------------------
    def end_card(self):
        name = T("StopProof", 86, INK, True).move_to(UP * 0.9)
        tag = T("Prove your AI agent actually stopped.", 36, BLUE, True).move_to(DOWN * 0.1)
        stack = T("Nebius Token Factory   NVIDIA Nemotron   LangSmith   Tavily   Tendem", 24, MID).move_to(DOWN * 1.1)
        repo = T("github.com/sanjay-ks23/Nebius_hackathon", 24, GREY, False, MONO).move_to(DOWN * 1.8)
        self.play(FadeIn(name, shift=UP * 0.2), run_time=0.7)
        self.play(FadeIn(tag), FadeIn(stack), FadeIn(repo), run_time=0.6)
        self.wait(2.6)
