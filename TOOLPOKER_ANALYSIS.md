# ToolPoker: Methodology Comparison & Novelty Analysis

Analysis of **ToolPoker** ("How Far Are LLMs From Professional Poker Players?
Revisiting Game-Theoretic Reasoning with Agentic Tool Use", ICLR 2026,
arXiv:2602.00528) in the context of this project's HU 200BB poker-LLM goal.
See `RESEARCH_DIRECTIONS.md` and `TRAINING_PIPELINE.md` for project state.

---

## 1. What ToolPoker actually proves

The paper is not just "tool use is nice." Its core empirical claim is a
**ceiling result**:

- They tried hard to make a policy-only LLM play GTO — behavior cloning (BC) on
  ~5k filtered expert reasoning traces, *then* PPO with a dense step-level
  regret reward derived from CFR (their "BC-RIRL" framework).
- It improved reasoning *style* (heuristic-reasoning and action-consistency
  scores rose) but **factual accuracy stayed broken and gameplay still lost to
  CFR**.
- Conclusion, verbatim: "LLMs alone cannot yet achieve both GTO actions and
  precise reasoning."
- The *only* thing that closed the gap to CFR was structurally taking the
  action from the solver at inference time (the ToolPoker framework: BC on
  tool-augmented traces + PPO with composite reward).

Two transferable findings:

1. **BC alone → superficial imitation; RL alone (no BC) → fails outright.**
   Both stages are needed, in that order.
2. **"Generalization without solvers" ablation:** remove the tool at inference
   and *HR/AC survive but FA (numeric accuracy) collapses first*. Qualitative
   strategic reasoning internalizes into weights; numbers do not.

ToolPoker's scope: Kuhn / Leduc / **Limit** Hold'em, tiny stacks (~50–99 chips,
blinds 1/2), **fixed bet sizes**. Solver is cheap enough to call live.

---

## 2. The biggest insight: our own benchmark already reproduces ToolPoker

From `RESEARCH_DIRECTIONS.md` §5:

| Run | aivat bb/100 |
|---|---|
| v2 smoke (policy only) | −87 |
| v2 + GTO tool injection | **−17.6** |
| LiveSolver (pure CFR) | ~−10 to 0 |

That ~70 bb/100 swing from injecting the tool **is** the ToolPoker finding, in
our game. We currently present tool injection as "ablation #2 of 5." ToolPoker
says it should be the **spine of the system**. Our data agrees with ToolPoker:
the policy-only path (1.3B SFT examples → strong weights) has a hard ceiling,
and we already have early evidence of it.

Unmade decision, explicit in neither doc: **are we training a policy, or
training a tool-caller?** TRAINING_PIPELINE is ~90% policy-training (SFT /
SIMPO / ORPO on 14 data types); tool use is a footnote. ToolPoker is the
inverse.

---

## 3. The asymmetry that justifies this project — and a hidden limitation

ToolPoker can call its solver *live, mid-hand*, because Limit Hold'em is tiny
(CFR+ tractable). We cannot — LiveSolver is ~140 s/hand. **That latency gap is
the entire reason the pre-solved combo dataset exists, and it is a legitimate
contribution.** Precise framing: we test whether a *precomputed / distilled*
GTO oracle can substitute for ToolPoker's *live* one.

Hidden limitation neither doc flags: our solves assume **fixed template
preflop ranges** (`btn_open`, `bb_call`, …). ToolPoker re-solves the *actual*
subgame. Our combo dataset is "GTO *given assumed ranges*." Against an opponent
whose preflop play differs, the cached strategy is off-tree. Either (a) scope
the paper claim to "GTO vs GTO-range opponents," or (b) plan node-locking
experiments. Shipping it as unqualified "GTO" is a reviewer magnet.

---

## 4. Tensions in the current pipeline, given ToolPoker's evidence

**1. Half the 14 data types are arguably anti-patterns.** Types 4, 5, 9, 12,
13, 14 train the model to *estimate* equity / EV / reach weight / matchups.
ToolPoker proves LLMs are bad at exactly this (FA stays low even after RL) and
that it is the thing to offload. Keep `eq`/`ev`/`w` as **tool outputs and CoT
grounding, not prediction targets.** High-value types are the *strategic* ones:
1/2/3/7/8 (action & mixed strategy) and 6/10/11 (range advantage, range
strategy, polarization) — "framework" knowledge the ablation shows *does*
internalize.

**2. The CoT plan will train the model to hallucinate numbers unless train/test
tool availability match.** TRAINING_PIPELINE Option A puts solver equities/EV
inside `<think>`. Inferring *without* the tool block trains the model to emit
numbers it cannot compute. Rule: **CoT that cites specific equities is only
honest if the tool is present at inference.** Toolless deployment → train CoT to
reason *qualitatively* (texture, range advantage, polarization), which the
ablation shows survives toolless.

**3. The pipeline does not structurally fix the knowing–doing gap** —
ToolPoker's headline diagnosis. SIMPO/ORPO on action labels with CoT-in-`chosen`
teaches "good action comes with reasoning," but at inference the model can still
emit good `<think>` then a bad `<action>`. ToolPoker eliminates the gap *by
construction* — the answer is lifted from the tool. Our closest structural fix
is type 8 / RESEARCH §7 (**output the full frequency distribution and sample**)
— promote it from "deferred ablation" to a candidate main design.

**4. Offline preference optimization cannot train real tool-calling.**
ToolPoker needed *online* PPO with a tool-execution reward + format reward —
SIMPO/ORPO have no analog. The "always-inject → LLM-initiated tool calls"
upgrade (RESEARCH step 5) is a much bigger lift than the doc implies; it likely
needs an RL stage, not a parser swap. To stay offline (SIMPO + self-play loop),
keep tool injection *always-on* rather than LLM-initiated.

---

## 5. Where this project genuinely extends ToolPoker

- **Harder game, real solver.** NL 200 BB postflop vs their Limit HE with
  50-chip stacks. Canonical-flop enumeration (1755 vs 22,100 via suit
  isomorphism) is a real systems contribution.
- **Per-combo *full mixed strategy* supervision.** ToolPoker's tool returns one
  GTO action + a few scalars. Combo-v2 exposes the entire range's mixing, both
  players' per-combo EV/equity, histograms. Strictly richer.
- **A *deterministic* factual-alignment metric.** ToolPoker's FA is a noisy
  GPT-4.1-mini LLM-judge score. We have ground-truth `eq[i]`/`ev[i]` per combo
  → check a trace's stated numbers programmatically. Methodological upgrade.
- **Borrow their Eq. 2 cheaply.** ToolPoker's regret-normalized step reward
  needs online RL. We have per-combo `ev` already — the EV gap between `chosen`
  and `rejected` *is* a regret signal. Use `|EV_chosen − EV_rejected|` as the
  SIMPO per-example margin (γ) or sample weight in `generate_pref_data.py`.
  Better than gating on frequency gap alone (a 70/30 freq split can be
  near-zero EV regret).

---

## 6. Concrete recommendations (prioritized)

1. **Make tool use the spine, not an ablation.** Restructure RESEARCH §6
   around: distilled qualitative policy + GTO oracle at inference. The
   −87 → −17.6 number is the headline.
2. **Add a reasoning-trace eval.** We only measure aivat. ToolPoker's second
   contribution is reasoning quality. Build the deterministic FA metric from
   ground-truth combo data.
3. **Re-weight `generate_pref_data.py` by EV-regret**, not just
   `--min-freq-gap`. Quick win.
4. **Decide CoT honestly:** tool-present inference → cite numbers; toolless →
   qualitative-only.
5. **Drop/downgrade numeric-estimation data types** (4/5/9/12/13/14) as
   *training targets*; keep as tool/CoT inputs.
6. **State the fixed-range limitation** explicitly and scope the claim, or plan
   node-locking.
7. **Promote frequency-distribution output (type 8)** to a primary design
   candidate — cleanest structural answer to the knowing–doing gap, novel vs
   both ToolPoker and SpinGPT (both output single actions).

Caution against over-rotating: ToolPoker is Limit HE with copy-the-solver
answers, so its "near-perfect AC" is partly trivial (action = tool output ⇒
consistency automatic), and its LLM-judge scores are noisy. Treat the *ceiling
result* and the *toolless ablation* as the transferable findings, not their
absolute reasoning numbers.

---

## 7. Where the TRUE novelty is — what ToolPoker overlooked

ToolPoker's title asks "how far are LLMs from **professional** players?" — but
what it delivers is a **GTO bot with a narrator**. Its agent is hard-capped at
solver quality (they admit ToolPoker ≈ CFR minus tool-call errors). Pros do not
win money by playing GTO — they win by *deviating from GTO to exploit*.
ToolPoker overlooked the actual definition of "professional." That gap, plus a
few others, is the novelty space.

Directions ranked by novelty × what our infrastructure uniquely enables.

### Direction 1 — Exploitative deviation from a GTO baseline (the biggest gap)

**What ToolPoker can't do:** its tool returns the GTO action, so the system is
structurally incapable of exceeding GTO. Against its own exploitable opponents
(NFSP/DQN/DMC), pure GTO leaves money on the table. ToolPoker plays the
unexploitable strategy, not the *maximally profitable* one. Nobody has built an
LLM that reasons about *how far and when to deviate* from equilibrium.

**Why we can and they couldn't:** CLAUDE.md lists a `node_locking` example —
a data generator for exploitation with no ToolPoker analog:

- Lock villain's range/strategy to a deviation (calls too much, never bluffs,
  over-folds turn) → re-solve → the new strategy is the **exploitative
  best-response**.
- Generate `(opponent_tendency → GTO_baseline → exploitative_adjustment)`
  triples at scale.
- Train the LLM to reason about the *delta*: "GTO bets 60% here; villain
  over-folds to turn barrels, so bet 100% and widen bluffs."

This reframes the knowing–doing gap: ToolPoker closed it *for GTO*. The
**exploitative knowing–doing gap is wide open.** It also puts the LLM in the one
lane the solver can't own — opponent adaptation. Boldest, highest-ceiling
direction. **Recommended paper spine.**

### Direction 2 — The internalization frontier (what must stay a tool)

**Overlooked:** ToolPoker's tool is real-time callable only because Limit HE is
tiny. They never confront the regime where the solver is too expensive to call
live — *our* regime. Their "generalization without solvers" ablation is one
paragraph.

**Novel question:** *which* GTO knowledge can an LLM internalize into weights,
and which is irreducibly external? We have the experimental matrix — the 14
data types span pure-numeric (equity/EV estimation) to pure-strategic (range
advantage, polarization). Train each, ablate tool availability, measure the
degradation curve. Deliverable: a principled map of the internalization
frontier for imperfect-info games, and a justification for distillation (cached
oracle) as a substitute for live solving. Lowest-risk, most aligned with what is
already built. **Recommended as the rigorous backbone section.**

### Direction 3 — Bet-sizing reasoning (a dimension ToolPoker has zero of)

Limit Hold'em has **fixed bet sizes**; ToolPoker never reasons about sizing.
NL 200 BB sizing (polar overbet vs merged small bet, 33% vs 75% vs 3x) is, per
CLAUDE.md, "the most teachable sizing decision for an LLM" — which is why rich
flop sizing was chosen. A paper on **LLM reasoning about bet sizing and
polarization** is novel by construction: no prior LLM+poker work could study it.
Mid-risk, clean. **Recommended to fold into the spine as the postflop testbed.**

### Direction 4 — Sequential range-narrowing as a trained LLM capability

ToolPoker reasons **per-spot** — no belief carried across streets. Combo-v2 has
villain's per-combo reach weight `w` at every node — the ground-truth range at
each point in the tree. Supervise, with exact labels, "after villain checks the
flop, their range is now X; after they bet turn it narrows to Y." Training an
LLM to do **sequential Bayesian range narrowing with ground-truth supervision**
connects to DeepStack/ReBeL's public-belief-state idea at the reasoning-trace
level — undone. Higher-risk (needs multi-turn data stitching, Tier-2 type 16).
Strong follow-up paper.

### Smaller open seams

- **Cost-aware / adaptive tool invocation.** ToolPoker calls the tool every
  hand because it's free for them. Ours costs 140 s. Train the model to call
  the solver *only* on close, high-variance spots (`ev` data flags near-zero-
  regret vs decisive). Adaptive tool use under cost is unexplored in
  imperfect-info games.
- **Deterministic factual-alignment metric.** (See §5.) Methodological upgrade
  over ToolPoker's noisy LLM-judge FA — claim it regardless of spine.

---

## 8. Recommended paper positioning

Make **Direction 1 (exploitative deviation)** the spine and fold in
**Direction 3 (sizing)** as the postflop testbed: *ToolPoker built a GTO
narrator; we build an agent that reasons about when and how to beat GTO, in a
game where the solver cannot be called live.* Use **Direction 2** as the
rigorous backbone section justifying the distillation architecture. Direction 4
is a strong follow-up if Direction 1 lands.

One-line positioning:

> **ToolPoker proved an LLM can faithfully *report* a solver. The open question
> is whether an LLM can reason *past* one — and that is the part that is
> actually "professional."**
