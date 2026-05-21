# Training Pipeline Design: LLM Poker from Combo-v2 Solver Data

Notes on turning the pre-solved combo-v2 dataset into LLM training data.
Covers the three-stage pipeline, the mixed-strategy problem, how reasoning
traces plug in, and how the GTO Wizard API dataset complements the solver data.
Append as decisions are made.

---

## 1. Preference Optimization Methods Compared

The data format `{prompt, chosen, rejected}` is identical for all three. The
objective is swapped at training time in `trl`.

| Method | Reference model | Memory | Notes |
|---|---|---|---|
| **DPO** | Required (SFT checkpoint) | Highest | Standard baseline; well-understood |
| **ORPO** | None (joint SFT+align loss) | Low | Penalizes 0%-freq actions within SFT pass |
| **SIMPO** | None | Low | Length-normalized reward + explicit margin γ; often beats DPO/ORPO (SimPO 2024) |

**Recommended: SIMPO** — no reference model, margin γ gives a hard separation
guarantee well-suited to hard poker constraints ("never fold the nuts"), and is
consistently stronger than DPO at the same memory budget.

Generate the preference pairs once; swap objective at training time.

---

## 2. Dataset Comparison: GTO Wizard API vs. Combo-v2 Solver

Two sources of training data — different strengths, not interchangeable.

### Quality comparison

| Dimension | GTO Wizard API (~30k pairs) | Combo-v2 solver (~1.3B potential) |
|---|---|---|
| Action precision | Near-perfect Nash | ~2% exploitability — functionally GTO |
| Mixed strategy info | ❌ Single action logged; freq unknown | ✓ Full distribution per combo |
| Per-hand equity / EV | ❌ None | ✓ Per-combo eq, EV, reach weight |
| Range context | ❌ None | ✓ range_eq, nut, strong, marginal, air |
| SIMPO pairs possible | ❌ No — can't identify 0%-freq actions | ✓ Yes — directly from freq distribution |
| Grounds reasoning traces | ❌ No context for "why" | ✓ Yes — equity + EV + range_advantage |
| Scale | 30k examples | ~1.3B (node, combo) pairs |
| Preflop coverage | ✓ Yes (real played hands) | ❌ Postflop only |
| Flop distribution | Non-uniform (real game skew) | Uniform over all 1755 canonical flops |

### The critical GTO Wizard limitation

You don't know if the logged action is *the* GTO action or one sample from a
mixed strategy. If GTO Wizard checks with KcKd, you can't tell whether:
- Check is 100% (pure) → the only correct action
- Check is 30% (mixed) → bet is equally valid; this was one sample

This makes GTO Wizard data **unsafe for SIMPO pairs** — you'd risk penalizing
valid mixed-strategy actions as "rejected." It also means you can't teach
correct mixing behavior from this data alone.

The 2% exploitability gap between the two solvers is **not a meaningful practical
difference** for LLM training. Both are functionally GTO. The distributional
richness of combo-v2 is the decisive advantage.

### Recommended role for each dataset

```
GTO Wizard 30k:
  → SFT phase 1: high-precision seed — 30k near-perfect single-action labels
  → Preflop coverage (unique — postflop solver doesn't have this)
  → Validation / benchmark: hold out ~5k examples as independent gold standard
    to measure whether the combo-v2-trained model generalizes correctly

Combo-v2 solver:
  → SFT phase 2 at scale: weighted examples teach correct mixing
  → SIMPO preference pairs: only source that can identify 0%-freq actions
  → Reasoning trace grounding: equity + EV + range context for CoT generation
```

**Note on GTO Wizard distribution**: collected from actual played hands, so it
overrepresents common game trees (SRP, K-high boards) and underrepresents rare
lines. If used alone at scale, this distribution skew would hurt generalization.
Combo-v2 covers all 1755 canonical flops uniformly — better strategic diversity.

---

## 3. The Mixed-Strategy Problem (Combo-v2)

**Core tension**: GTO poker requires *mixed* strategies (e.g., KcKd: bet 70% /
check 30%). Training on `chosen = argmax(strategy)` teaches a pure-strategy
approximation that is exploitable.

Current `scripts/generate_pref_data.py` picks `chosen = highest-freq action`,
`rejected = most-contrasting 0%-freq action`. Combos with no 0%-freq action
(e.g., KcKd where both bet and check are played) are **skipped** — this is the
safe behavior (we never punish a valid action), but it means pure-strategy bias
creeps in for the combos that *are* included.

### Do we need a separate SFT generator?

Depends on the training objective:

- **ORPO**: No separate SFT step needed. ORPO is a joint SFT + alignment loss —
  it increases probability of `chosen` while penalizing `rejected` in one pass.
  `generate_pref_data.py` output is sufficient.

- **SIMPO**: Expects an SFT base model first. But no separate generator needed
  either — just extract `chosen` from existing pref pairs:
  ```bash
  jq -c '{prompt: .prompt, response: .chosen}' data/pref_training.jsonl \
    > data/sft_training.jsonl
  ```

- **Teaching mixed strategies** (the one gap): KcKd (bet 70% / check 30%) is
  **skipped** by `generate_pref_data.py` because neither action has 0% frequency.
  A dedicated `generate_sft_data.py` would emit that combo twice — `bet` at 70%
  weight and `check` at 30% — so the model learns to actually mix. This is what
  SpinGPT does before its ORPO pass.

  **Whether this matters in practice**: against non-GTO humans, pure-strategy
  approximation costs very little EV. Against a GTO opponent over a long session,
  it is exploitable in principle. `generate_sft_data.py` is therefore lower
  priority than getting SIMPO/ORPO working end-to-end.

**Recommended**: start with ORPO (single pass, no SFT phase) or SIMPO with
`chosen` extracted from pref pairs as the SFT seed. Add `generate_sft_data.py`
later if benchmark results show exploitability from pure-strategy bias.

---

## 4. Reasoning Traces: How They Plug In

Without reasoning, the model sees:
```
prompt:   "...Legal actions: check, bet 1250.\n...Your optimal action is:"
chosen:   "<action>bet 1250</action>"
rejected: "<action>check</action>"
```

It learns stimulus → response but has **no visible reasoning** and will not
reliably use the GTO SOLVER CONTEXT block at inference.

### Option A — CoT in `chosen` only (recommended first step)

```
chosen:   "<think>
             I have AcAd — the nuts on a 2c2d2h board. OOP has range advantage
             (61% vs 39%). My equity is 80% and EV is 4100 chips. Checking
             gives away a free card when I'm already ahead — the solver confirms
             betting captures more EV. Bet for value.
           </think>
           <action>bet 1250</action>"
rejected: "<action>check</action>"
```

The `<think>` block is generated by a teacher model (GPT-4o or ≥70B) with
**solver stats force-fed into its prompt**. The teacher is not asked to compute
equity — it receives the equity, EV, and range_advantage from the solver and
writes a coherent justification for the correct action. This prevents
hallucination: the teacher can't get numbers wrong because you gave them.

The SIMPO loss then simultaneously:
1. Increases probability of CoT + correct action
2. Decreases probability of bare wrong action

The model learns that correct actions come *with reasoning*. At inference, it
samples `<think>...</think><action>...</action>`.

### Option B — CoT in both (harder, more powerful, later)

```
chosen:   "<think>I have the nuts, bet for value...</think><action>bet 1250</action>"
rejected: "<think>Board is paired, opponent might have a boat, maybe check...</think><action>check</action>"
```

Rejected reasoning requires the teacher to argue convincingly *for* the wrong
action — frontier models resist this, and it's expensive to generate. Defer to
a later iteration.

### Key insight (from ToolPoker paper)

The model needs to see reasoning that **references** the solver numbers, not just
see the numbers. The CoT teaches it to use equity and EV context rather than
ignore it. Without CoT distillation, injecting the SOLVER CONTEXT block at
inference has diminishing returns because the model never learned to condition
on those numbers during training.

### Generation strategy for CoT (Option A)

Target ~50k high-entropy, high-information spots:
- `entropy(range_strategy) > 0.5` (genuinely mixed node, not trivial)
- Chosen action is bet or raise (value / bluff decisions — most instructive)
- Node is not a trivially dominated spot

Pipeline:
```python
for pref_pair in pref_training.jsonl:
    if entropy(rec["range_strategy"]) > 0.5:
        think_block = call_teacher(
            system="Write a brief <think>...</think> GTO reasoning for the action below. Use the solver stats provided.",
            prompt=pref_pair["prompt"],        # already contains GTO SOLVER CONTEXT
            suffix=f"The correct action is: {strip_tags(pref_pair['chosen'])}",
        )
        pref_pair["chosen"] = think_block + "\n" + pref_pair["chosen"]
```

Teacher requirement: 70B minimum for consistent strategic logic; GPT-4o /
frontier recommended for factual accuracy without external tools.

---

## 5. Full Recommended Pipeline

```
Stage 0 — Data generation (offline, one-time):
  generate_pref_data.py (done)    → data/pref_training.jsonl   (combo-v2 pref pairs)
  GTO Wizard 30k pairs            → data/sft_gtow.jsonl        (existing; split off ~5k as val)
  [hold out ~5k GTO Wizard pairs] → data/gtow_val.jsonl        (independent benchmark)

  # Optional later — only if mixing exploitability shows up in benchmarks:
  generate_sft_data.py  (done)    → data/sft_combo.jsonl       (weighted SFT for mixing)

Stage 1 — ORPO (simplest path, single pass):
  Train on: pref_training.jsonl
  → joint SFT + alignment in one pass; no separate SFT model needed
  → GTO Wizard 30k can be mixed in as additional chosen-only SFT signal

  — OR —

Stage 1a — SFT (if using SIMPO):
  Train on: sft_gtow.jsonl (30k, high-precision, includes preflop)
          + chosen field extracted from pref_training.jsonl
  → establishes SFT base model

Stage 1b — SIMPO:
  Train on: pref_training.jsonl
  → penalizes 0%-freq catastrophic actions on top of SFT base

Stage 2 (optional) — CoT distillation:
  add_reasoning.py (TODO): GPT-4o generates <think> for ~50k high-entropy spots
  Re-run ORPO/SIMPO on CoT-augmented subset
  → model learns to reference equity / EV / range context in its reasoning

Stage 3 (optional) — Iterative self-play:
  Play model vs. GTO Wizard → label mistakes with solver
  Add mistakes to SIMPO/ORPO rejected set → re-align → repeat

Stage 4 — Validation:
  Evaluate on gtow_val.jsonl (independent GTO Wizard gold standard)
  → measures whether combo-v2-trained model generalizes to near-perfect Nash decisions
  → also run full aivat_bb/100 vs. LiveSolver
```

---

## 6. Scripts and Status

| Script | Status | Purpose |
|---|---|---|
| `scripts/generate_pref_data.py` | **Done** | Combo-v2 → SIMPO/ORPO/DPO preference pairs; `--min-freq-gap 0.5` enables soft pairs for mixed-strategy combos |
| `scripts/generate_sft_data.py` | **Done** | Combo-v2 → weighted SFT; `--actions-per-combo 5` emits all actions proportionally; street-rebalanced (flop 1.0 / turn 0.5 / river 0.1) |
| `scripts/generate_equity_data.py` | **Done** | Combo-v2 → equity estimation (`--task equity`) and hand-strength classification (`--task strength`); no solver context in prompt |
| `scripts/add_reasoning.py` | TODO | Add GPT-4o CoT traces to existing pref pairs |
| `scripts/decode_combo_data.py` | Done | Streaming reader for combo-v2 .jsonl.zst |

GTO Wizard 30k dataset lives in `cs153-project/` — already collected, needs
to be split into train / val before use.

---

## 7. Alternative: Train on Full Frequency Distributions

Instead of single-action output, train the model to output the full mixed
strategy directly:

```
<strategy>bet 1250: 70% | check: 30%</strategy>
```

At inference, parse and sample from the distribution to achieve true GTO mixing.

**Advantages over current approach:**
- Uses 100% of combo-v2 data — no combos skipped due to mixed strategies
- Teaches true GTO mixing in one pass; no weighted SFT workaround needed
- Output is interpretable (visible mixing ratios)
- Novel vs. SpinGPT / ToolPoker which both output single actions — potential paper contribution

**Challenges:**
- Numerical hallucination risk (SpinGPT flagged `5.11 > 5.2` errors; simpler here
  since model regurgitates training numbers rather than computing them, but still a risk)
- SIMPO interaction: chosen string is longer than a single-action rejected string;
  SIMPO's length-normalization could bias the reward against the chosen response.
  Would need empirical check or reward scaling fix.
- Inference pipeline change: downstream parser + categorical sampler needed;
  PokerBench / GTO Wizard eval harness expects a single action and needs updating.
- Pure-strategy nodes work naturally (`<strategy>bet 1250: 100%</strategy>`) — no
  special casing.

**Decision**: deferred. Validate single-action pipeline first; add frequency-output
as an ablation once benchmark baseline is established. It is a one-time architectural
choice that affects training format, inference, and all eval code — better to commit
to it deliberately after the first results are in.

---

## 8. Open Questions

- **Action format at inference**: current output is `<action>bet 1250</action>`.
  Does the inference API / benchmarking harness parse this? Verify before
  training at scale.
- **SFT weighting**: should reach weight `w` or action freq or `w × freq` be
  the SFT sample weight? `w × freq` is most principled (importance-weighted by
  both reach and action).
- **SIMPO margin γ**: start with `γ = 0.5` (SimPO paper default). May need
  tuning — higher γ enforces harder separation, useful for the "never fold nuts"
  constraint.
- **CoT length budget**: `<think>` blocks should be kept short (3–5 sentences)
  to avoid inflating sequence length and slowing training. The SOLVER CONTEXT
  block already provides all grounding; the CoT just needs to connect it to the
  action.

---

## 9. Full Training Data Taxonomy

Everything derivable from the combo-v2 solver data (~178 GB compressed,
5,247 files, ~1.3B potential (node, combo) pairs).

**Complete field inventory** (confirmed by live inspection):

| Source | Fields | Currently used |
|---|---|---|
| Header | `matchup`, `flop`, `starting_pot`, `effective_stack`, `combos_oop/ip` | Prompt construction ✓ |
| Node | `history`, `board`, `to_act`, `pot`, `eff_stack`, `spr`, `actions` | Prompt construction ✓ |
| Node | `range_advantage`, `nut_advantage` | Type 6 ✓ |
| Node | `range_strategy` | Entropy filter only — **not trained on** |
| Node | `oop/ip.{range_eq, nut, strong, marginal, weak, air}` | Solver context block ✓ |
| Node | `oop/ip.hist[10]` | **Completely unused** |
| Per-combo (actor) | `idx → hand`, `eq`, `strategy` | Types 1–8, equity ✓ |
| Per-combo (actor) | `ev` | Solver context only — **not trained on as target** |
| Per-combo (actor) | `w` | Sampling weight only — **not trained on** |
| Per-combo (non-actor) | `idx`, `eq`, `w`, `ev` | **Completely unused** |

### Tier 1 — Direct from data, no external model

| # | Data type | Source field(s) | Output format | Teaches | Est. volume |
|---|---|---|---|---|---|
| 1 | **Preference pairs (0%-freq)** | `strategy` | `{prompt, chosen, rejected}` | Hard GTO constraints — never fold nuts | ~50M |
| 2 | **Soft preference pairs** | `strategy` | `{prompt, chosen, rejected}` | Directional preference for mixed combos | ~85M |
| 3 | **Weighted SFT** | `strategy`, `w` | `{prompt, response=action}` | Correct mixing frequencies | ~120M |
| 4 | **Equity estimation** | `eq` (actor) | `{prompt, "67.3%"}` | Hand-reading equity without oracle | ~100M |
| 5 | **Hand strength classification** | `eq` (actor) | `{prompt, "strong"}` | Categorical hand assessment | ~100M |
| 6 | **Range advantage classification** | `range_advantage`, `nut_advantage`, `range_eq` | `{prompt, "OOP has range advantage"}` | Board-level range reading | ~45k (per-node) |
| 7 | **Bet sizing pairs** | `strategy` (multi-action nodes) | `{prompt, chosen=small_bet, rejected=large_bet}` | Polarized vs. merged sizing | subset of #1 |
| 8 | **Frequency distribution output** | `strategy` | `{prompt, "bet 1650: 70% \| check: 30%"}` | Full mixed strategy — no information loss | ~100M |
| 9 | **EV estimation** | `ev` (actor) | `{prompt, "2847 chips"}` | Chip EV reasoning independent of equity | ~100M |
| 10 | **Range strategy prediction** | `range_strategy` | `{prompt, "check: 40%, bet 1650: 35%, bet 3750: 25%"}` | Range-level strategy without knowing specific hand | ~45k (per-node) |
| 11 | **Equity histogram / polarization** | `oop.hist[10]`, `ip.hist[10]` | `{prompt, "OOP range: 15% dead equity, 28% marginal..."}` | Full equity distribution; distinguishes polarized vs. merged ranges | ~45k (per-node) |
| 12 | **Reach weight / hand frequency** | `w` (actor) | `{prompt, "This hand appears in your range 3.2% of the time here"}` | Bayesian hand-frequency reasoning; how often am I "here" with this hand | ~100M |
| 13 | **Non-acting player EV** | `ev` (non-actor) | `{prompt, "From IP's perspective facing this bet, IP's EV is X chips"}` | Opponent's expected payoff; teaches pot equity modeling from both sides | ~100M |
| 14 | **Cross-player equity comparison** | `eq` (both actors at same node) | `{prompt="OOP has KcKd, IP has AhJd on Kh7d2c", response="OOP leads 65% vs 35%"}` | Head-to-head hand matchup reasoning | ~10M (paired combos) |

**Gap analysis — what the 14 types cover vs. the raw fields:**

- `strategy` → types 1, 2, 3, 7, 8 ✓ (lossless via type 8)
- `eq` (actor) → types 4, 5, 14 ✓
- `ev` (actor) → type 9 ✓
- `w` (actor) → type 12 ✓
- `range_advantage/nut_advantage` → type 6 ✓
- `range_strategy` → type 10 ✓
- `oop/ip.hist[10]` → type 11 ✓
- `eq/ev/w` (non-actor) → types 13, 14 ✓
- `spr` — fully determined by pot + eff_stack already in prompt; no new info
- `flop_idx`, `matchup` — embedded in context; no standalone training value

**Conclusion: types 1–14 achieve lossless coverage of the combo-v2 data.**

### Tier 2 — Moderate effort, external dependencies

| # | Data type | Output | Teaches | Notes |
|---|---|---|---|---|
| 15 | **EV reasoning traces** | `<think>equity+EV reasoning</think>\n<action>` | Use solver context in reasoning chain | GPT-4o generates `<think>`; solver stats force-fed to prevent hallucination |
| 16 | **Multi-turn hand playthrough** | Multi-turn chat through flop→turn→river | Cross-street range construction | Needs tree-path stitching across records |

### Tier 3 — Deferred (new infrastructure required)

| # | Data type | Blocker |
|---|---|---|
| 17 | Per-action EV delta ("betting wins +240 vs checking") | `ev[j]` is EV under GTO strategy, not per-action; needs cross-record indexing |
| 18 | Opponent range inference across streets | Tree-path stitching; ranges narrow as action history grows |
| 19 | Exploitability estimation | Not in data; requires counter-strategy computation |

---

## 10. Data Quality Filtering

Raw volume at 100% sample: ~500M+ examples across all types. Not all examples
have equal training value. Below are pruning strategies ranked by impact.

### High-impact filters (apply first)

**Street rebalancing** — river nodes are 90% of raw data but flop/turn nodes
carry the highest strategic learning signal (decisions here cascade across
subsequent streets). Oversample flop and turn:
```
flop:  sample_rate = 1.0   (keep all — only ~24 nodes per SRP file)
turn:  sample_rate = 0.5
river: sample_rate = 0.05–0.1
```
This alone cuts volume ~10× while preserving strategic diversity.

**Equity range filter** — the most judgment-requiring spots are marginal-to-strong
hands (0.25 < eq < 0.75). Pure nuts (eq > 0.95) and pure air (eq < 0.05) are
often trivial (always bet for value / always fold to aggression). Filter or
heavily downsample extreme-equity combos:
```python
if eq < 0.05 or eq > 0.95:
    if rng.random() > 0.1:   # keep 10% of trivial spots
        skip
```

**Strategy entropy filter** — already implemented (`--min-entropy 0.1`). Nodes
where the entire range plays one action have no per-hand discrimination signal.
Extend to per-combo level: skip combos with pure strategies at high-entropy nodes
(the mixed combos at that node carry the real signal).

**Action space filter** — nodes with 3+ actions (sizing decisions) are highest
value; a model that can choose between bet 33% and bet 75% understands polarization.
Prioritize: `len(actions) >= 3` gets higher sample rate.

### Medium-impact filters

**Reach weight floor** — already `--min-weight 0.005`. The effective floor
eliminates hands that appear <0.5% of the time in that spot. Consider raising
to 0.01 for efficiency without meaningful coverage loss.

**Matchup balance** — SRP is ~60% of files but covers the widest range of board
textures. 4BP has very narrow ranges (QQ+, AKs/o) so many combos are trivially
dominated. Suggested weights: SRP 50%, 3BP 30%, 4BP 20%.

**EV magnitude filter** (types 9, 13) — skip examples where |EV - pot/2| < 50
chips (nearly zero-EV spots where any action is roughly breakeven). These add
noise without teaching anything about decision-making.

**Frequency gap floor** (types 1, 2) — already `--min-freq-gap`. For soft pairs,
minimum gap of 0.5 ensures a meaningful preference signal. Below 0.3, penalizing
the lower-frequency action risks confusing the model.

### Practical target sizes

| Use case | Target examples | Strategy |
|---|---|---|
| Initial SFT run | 100k–500k | 4BP only, all streets, no filters beyond weight/entropy |
| Full SFT baseline | 1M–5M | All matchups, 10% sample rate, street-rebalanced |
| SIMPO alignment | 500k–2M | Pref pairs only, high-entropy nodes, marginal-equity combos |
| Equity auxiliary task | 500k | Equity estimation + hand classification, balanced by equity bucket |
| Paper ablation set | 100k each | Matched sets for fair comparison across data types |

### What NOT to filter out

- **Near-zero-EV spots** for action prediction (types 1–3): the model needs to
  know when to check-call even at breakeven EV — these teach pot-odds reasoning.
- **Pure-strategy combos** at mixed-strategy nodes: even if combo A always bets,
  the contrast with combo B (which checks) at the same node is the signal.
- **Rare hands** (low reach weight) completely: keeping a small fraction ensures
  the model handles unusual holdings rather than only learned from common ones.
