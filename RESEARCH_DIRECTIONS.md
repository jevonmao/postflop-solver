# Research Directions: LLM + GTO Solver for HU 200BB Poker

Notes for paper writing and future work. Covers landmark results, current project
state, and the key insight about using the pre-solved combo dataset for LLM
supervision.

---

## 1. Landmark Research

| Work | Year | Core Contribution |
|---|---|---|
| **DCFR** | 2019 | Discounted CFR — discounts early regrets for faster Nash convergence. Foundation of this solver. [arXiv:1809.04040](https://arxiv.org/abs/1809.04040) |
| **DeepStack** | 2017 | Continual re-solving + neural value functions; search during play. [arXiv:1701.01724](https://arxiv.org/abs/1701.01724) |
| **Libratus** | 2018 | Superhuman HU via nested subgame solving + automated abstraction. |
| **ReBeL** | 2020 | Generalizes AlphaZero to imperfect-info games via Public Belief States (PBS). [arXiv:2007.13544](https://arxiv.org/abs/2007.13544) |
| **Student of Games** | 2021 | Unified GT-CFR for perfect + imperfect-info (Chess, Go, Poker, Stratego). [arXiv:2112.03178](https://arxiv.org/abs/2112.03178) |

---

## 2. Recent LLM-Specific Work (2025–2026)

### SpinGPT (Sept 2025) — [arXiv:2509.22387](https://arxiv.org/abs/2509.22387)
- **Base**: Llama-3.1-8B-Instruct
- **Pipeline**: (1) SFT on 320k high-stakes human decisions (€50–€250); (2) ORPO on 270k solver-generated (InstaGTO) + human hands
- **Results**: 13.4 BB/100 win rate vs Slumbot; 78% GTO tolerant accuracy
- **Limitations**: Numerical hallucinations (`5.11 > 5.2`); catastrophic forgetting of human nuance if alignment data not balanced
- **Prompt format**: Compressed text — `pos:H=BTN stacks:H=25... hand:AcKc | pre:H r2...` — token efficiency is critical for CoT headroom

### ToolPoker (Feb 2026) — [arXiv:2602.00528](https://arxiv.org/abs/2602.00528)
- **Core insight**: Solves the *Knowing-Doing Gap* — LLMs can articulate correct GTO logic but choose the wrong action
- **Method**: Agentic tool use: `<think> → <tool_call> → <answer>` trace; LLM calls CFR solvers and equity calculators within its reasoning
- **Results**: Near-perfect Reasoning Scores; SOTA gameplay by offloading math to tools
- **Key takeaway**: Inference-time tool use can substitute for scale in training data, IF the model is taught to use and reason about tool outputs

### PokerBench (Jan 2025) — [arXiv:2501.08328](https://arxiv.org/abs/2501.08328)
- **Benchmark**: 11k scenarios (1k preflop, 10k postflop); GPT-4 zero-shot achieves only ~53% GTO accuracy
- **Findings**: SFT improves accuracy sharply but risks over-passivity or over-aggression depending on data distribution

---

## 3. Training Techniques

### Preference Optimization Comparison

| Method | Reference model | Memory | When to use |
|---|---|---|---|
| **DPO** | Required (SFT checkpoint) | Highest | Standard baseline; well-understood |
| **ORPO** | None (joint SFT+align loss) | Low | Penalizes 0%-freq actions within SFT pass |
| **SIMPO** | None | Low | Length-normalized reward + explicit margin γ; often beats DPO/ORPO; simpler |

**Recommended: SIMPO** for this project — no reference model (same GPU budget as ORPO), margin γ
gives a hard separation guarantee (critical: "never fold the nuts"), and SimPO (2024) consistently
outperforms DPO. All three use the identical `{prompt, chosen, rejected}` data format — generate
once, swap objective at training time via `trl.SimPOTrainer` / `ORPOTrainer` / `DPOTrainer`.

### ORPO (Odds Ratio Preference Optimization)
- Reference-model-free → lower GPU memory than DPO/PPO
- Simultaneously increases likelihood of "chosen" (GTO) action while penalizing "rejected" (suboptimal) via odds-ratio penalty
- **Critical for poker**: prevents model from ever choosing 0%-frequency actions ("never fold the nuts")
- Naturally generates preference pairs: chosen = highest-freq action, rejected = 0%-freq actions

### Weighted SFT
- If solver says `call 80% / raise 20%`, include both actions at that ratio
- Simpler than ORPO but ORPO generally more effective at suppressing rare-but-catastrophic actions

### Reasoning Trace Distillation ("The Why")
- **Teacher size requirement**: 70B minimum for consistent strategic logic; 400B+ / frontier (GPT-4o/o1) for high factual accuracy without external tools
- **Hybrid approach (recommended)**: Use a 70B model to generate reasoning, but **force-feed** the GTO action and equity stats from the solver into the prompt — prevents hallucinated justifications
- The solver already provides all grounding inputs: `range_eq`, `nut`, `range_advantage`, `equity`, `ev`, `actions[]`

### Iterative Self-Play Loop (2025–2026 trend)
```
SFT on corrected data
  → Play N hands vs GTO Wizard
  → Label model's mistakes with solver
  → Mistakes become "rejected" in ORPO
  → Re-align
  → Repeat
```
Infrastructure for this loop is already in place (`pokerbench_api.py` + solver).

---

## 4. The Combo Dataset as Supervision Data

### Scale
The pre-solved `data/solves_combo/` dataset (combo-v2 format) contains per-combo
GTO strategies at every decision node across all 1755 canonical flops × 3
matchups. The (node, combo) pair count is approximately:

```
1755 flops × 3 matchups × 8 turn samples × 6 river samples
× ~10 decision nodes/runout × ~500 active combos/node
≈ 1.3 billion (board, history, hero_hand) → GTO_action examples
```

Sampling comparison vs. SpinGPT (320k decisions):

| Sample rate | Examples | vs. SpinGPT |
|---|---|---|
| 0.01% | ~126k | 0.4× |
| 0.1% | ~1.3M | **4× SpinGPT** |
| 1% | ~12.6M | **39× SpinGPT** |

All at perfect solver quality with no human noise, no labeling cost.

### What the combo-v2 format provides per training example

| Field | Training use |
|---|---|
| `combo_data.strategy[i]` | Exact per-hand action frequencies → weighted SFT or ORPO pairs, zero labeling |
| `combo_data.eq[i]` | Hero's equity → grounds reasoning: "I have 68% equity..." |
| `combo_data.ev[i]` | EV in chips → grounds reasoning: "checking loses 240 chips vs betting" |
| `range_eq`, `nut`, `range_advantage` | Range-level reasoning: "OOP has range advantage on K72..." |
| `actions[]` | Exact GTO action menu → constrain model to in-tree moves |
| `combo_data.w[i]` | Reach weight → importance-weight training examples |

The bottleneck is no longer data quantity — it is:
1. **Prompt format**: encoding `(board, history, pot, hand)` token-efficiently
2. **Sampling strategy**: weight by `w[i]` (reach probability) and action entropy; ignore pure-strategy trivial nodes
3. **Reasoning traces**: generate CoT for a subset of high-entropy spots using GPT-4o with solver stats injected
4. **Strategic diversity**: 1755 flops × suit isomorphism → ensure training distribution covers full strategic space, not over-indexed on K-high boards

### Data generator sketch
```python
for spot in iterate_spots(file):          # stream from .jsonl.zst
    if spot['to_act'] == hero_player:
        for j, combo_idx in enumerate(cd['oop']['idx']):
            hand = header['combos_oop'][combo_idx]
            freqs = normalize_strategy(cd['strategy'][j], spot['actions'])
            weight = cd['oop']['w'][j]
            equity = cd['oop']['eq'][j]
            ev     = cd['oop']['ev'][j]
            # emit: (board, history, hand, pot, equity, ev) → (action, freqs)
```

This is the inverse of `gto_lookup/` — instead of querying the DB during play,
stream it for training.

---

## 5. Current Project State vs. SOTA

| Dimension | Current state | Target |
|---|---|---|
| Data volume | ~17k SFT examples (from ~5k GTO Wizard hands) | 1M+ from combo dataset |
| Training objective | Plain SFT (v1 broken; v2 corrected data in progress) | SFT → ORPO → iterative self-play |
| Reasoning | Action labels only | CoT traces grounded in solver math |
| Tool use | Always-inject oracle (`--enable-tools`, −17.6 aivat) | LLM-initiated tool calls (ToolPoker style) |
| Iterative refinement | Not yet | Self-play loop with solver labeling |

### Benchmark results so far (HU 200BB vs GTO Wizard, aivat_bb/100)

| Run | Model | aivat_bb/100 | Notes |
|---|---|---|---|
| Baseline | broken v1 LoRA | −55.6 | post-action state leakage in training data |
| Inference fix only | broken v1 LoRA + prompt fix | −53.1 | street suffix + blind tokens; SB-open fold 81%→57% |
| Smoke retrain | v2 smoke (300 steps, corrected data) | −87 | model now conditions on hole cards; aivat worse due to undertrained aggression |
| GTO tool injection | smoke + `--enable-tools` | **−17.6** | 20 hands, noisy; oracle injection on every decision |
| True GTO upper bound | LiveSolver (Rust CFR) | ~−10 to 0 | 42 hands in progress; ~140s/hand |

### Critical bug found and fixed
Training data recorded post-action pot/stack (after GTOW's action). At inference,
the API serves pre-action state. The model memorized (pot, stack) → action as a
lookup table — hole cards became irrelevant noise. Fixed by
`scripts/fix_post_action_drift.py`, verified by smoke retrain showing hand-conditioned play.

---

## 6. Recommended Paper Narrative

**Claim**: A pre-solved CFR dataset at this scale, combined with per-combo strategy
extraction, produces supervision signal that (a) matches or exceeds frontier LLM
training data in quantity, and (b) is richer in strategic grounding than
human-play datasets used by SpinGPT and similar work.

**Contribution positioning**:
- vs. SpinGPT: similar architecture but (1) pre-action state training data, (2) combo-level per-hand oracle, (3) GTO tool injection at inference
- vs. ToolPoker: same tool-use direction but with a self-generated CFR dataset rather than API calls to external solvers during training
- Novel: the combo-v2 format as a training data source; scale analysis of 1.3B (node, hand) supervision examples from 1755 canonical flops

**Ablations to run** (for paper):
1. SFT (corrected data) vs. SFT + ORPO
2. With vs. without GTO tool injection at inference
3. With vs. without reasoning traces
4. LLM-initiated tool calls vs. always-inject oracle
5. Full combo dataset sample vs. small GTO Wizard hand dataset

---

## 7. Next Steps (priority order)

1. **Benchmark v2 retrain** — once training finishes, run 200-hand comparison: v2-alone vs. v2+tools vs. LiveSolver
2. **Weighted SFT data** — `scripts/generate_sft_data.py` (TODO): stream combo-v2 into SFT examples weighted by `reach × action_freq`; teaches correct mixed strategies
3. **SIMPO preference pairs** — `scripts/generate_pref_data.py` (done): stream combo-v2 into SIMPO/ORPO/DPO preference pairs; suppresses 0%-freq catastrophic actions
4. **Reasoning trace generation** — `scripts/add_reasoning.py` (TODO): for ~50k high-entropy spots, prompt GPT-4o with board + range stats + forced correct action → generate `<think>` CoT traces; augment pref pairs
5. **Upgrade tool-calling** — convert from always-inject to LLM-initiated (`--tool-call-parser llama3_json` on vLLM)
6. **Iterative self-play loop** — play v2+SIMPO, label mistakes with solver, add to SIMPO rejected set, repeat

See `TRAINING_PIPELINE.md` for full pipeline design, the mixed-strategy problem, and how CoT traces integrate.
