# postflop-solver

> [!IMPORTANT]
> **As of October 2023, I have started developing a poker solver as a business and have decided to suspend development of this open-source project. See [this issue] for more information.**

[this issue]: https://github.com/b-inary/postflop-solver/issues/46

---

An open-source postflop solver library written in Rust

Documentation: https://b-inary.github.io/postflop_solver/postflop_solver/

**Related repositories**
- Web app (WASM Postflop): https://github.com/b-inary/wasm-postflop
- Desktop app (Desktop Postflop): https://github.com/b-inary/desktop-postflop

**Note:**
The primary purpose of this library is to serve as a backend engine for the GUI applications ([WASM Postflop] and [Desktop Postflop]).
The direct use of this library by the users/developers is not a critical purpose by design.
Therefore, breaking changes are often made without version changes.
See [CHANGES.md](CHANGES.md) for details about breaking changes.

[WASM Postflop]: https://github.com/b-inary/wasm-postflop
[Desktop Postflop]: https://github.com/b-inary/desktop-postflop

## Usage

- `Cargo.toml`

```toml
[dependencies]
postflop-solver = { git = "https://github.com/b-inary/postflop-solver" }
```

- Examples

You can find examples in the [examples](examples) directory.

If you have cloned this repository, you can run the example with the following command:

```sh
$ cargo run --release --example basic
```

## Implementation details

- **Algorithm**: The solver uses the state-of-the-art [Discounted CFR] algorithm.
  Currently, the value of γ is set to 3.0 instead of the 2.0 recommended in the original paper.
  Also, the solver resets the cumulative strategy when the number of iterations is a power of 4.
- **Performance**: The solver engine is highly optimized for performance with maintainable code.
  The engine supports multithreading by default, and it takes full advantage of unsafe Rust in hot spots.
  The developer reviews the assembly output from the compiler and ensures that SIMD instructions are used as much as possible.
  Combined with the algorithm described above, the performance surpasses paid solvers such as PioSOLVER and GTO+.
- **Isomorphism**: The solver does not perform any abstraction.
  However, isomorphic chances (turn and river deals) are combined into one.
  For example, if the flop is monotone, the three non-dealt suits are isomorphic, allowing us to skip the calculation for two of the three suits.
- **Precision**: 32-bit floating-point numbers are used in most places.
  When calculating summations, temporary values use 64-bit floating-point numbers.
  There is also a compression option where each game node stores the values by 16-bit integers with a single 32-bit floating-point scaling factor.
- **Bunching effect**: At the time of writing, this is the only implementation that can handle the bunching effect.
  It supports up to four folded players (6-max game).
  The implementation correctly counts the number of card combinations and does not rely on heuristics such as manipulating the probability distribution of the deck.
  Note, however, that enabling the bunching effect increases the time complexity of the evaluation at the terminal nodes and slows down the computation significantly.

[Discounted CFR]: https://arxiv.org/abs/1809.04040

## Crate features

- `bincode`: Uses [bincode] crate (2.0.0-rc.3) to serialize and deserialize the `PostFlopGame` struct.
  This feature is required to save and load the game tree.
  Enabled by default.
- `custom-alloc`: Uses custom memory allocator in solving process (only available in nightly Rust).
  It significantly reduces the number of calls of the default allocator, so it is recommended to use this feature when the default allocator is not so efficient.
  Note that this feature assumes that, at most, only one instance of `PostFlopGame` is available when solving in a program.
  Disabled by default.
- `rayon`: Uses [rayon] crate for parallelization.
  Enabled by default.
- `zstd`: Uses [zstd] crate to compress and decompress the game tree.
  This feature is required to save and load the game tree with compression.
  Disabled by default.

[bincode]: https://github.com/bincode-org/bincode
[rayon]: https://github.com/rayon-rs/rayon
[zstd]: https://github.com/gyscos/zstd-rs

## Dataset pipeline progress log

### 2026-05-20 — combo-v2 format + full cluster run kicked off

Rebuilt the per-combo data emission to be ~20–100× smaller and decodable
without a Rust runtime. Then validated the format end-to-end and submitted
the full HU 200 BB dataset run on Stanford SC.

**Format rewrite (`feat(dataset): combo-v2 …`):**
- Sparse per-node arrays — combos with reach weight < 1e-6 are omitted
  (parallel `idx` / `eq` / `w` / `ev`).
- Quantization — equity & weights to 3 decimals, EV rounded to integer chips.
- Strategy compression — per-combo distributions stored as a single `int`
  (action index) when one action holds ≥99.5% of the weight; full float array
  otherwise.
- File-level header line embeds the canonical OOP/IP combo enumeration as
  `"AcAd"`-style strings, so downstream consumers map `idx → hand` without
  rerunning the solver.
- Output written as `.jsonl.zst` (level 6, multithreaded). Driver requires
  `--features zstd`; `run_production.sh` toggles this automatically when
  `COMBO_DATA=1`.
- Reference Python decoder at `scripts/decode_combo_data.py` (pure Python,
  needs only the `zstandard` package).
- Skip-check keys on `schema=combo-v2` in the meta file, so any pre-existing
  v1 files are re-solved cleanly.

**Local validation:** 4BP `AcAdAh` root spot — 4128 records, 6.5 MB compressed
(was ~120 MB dense), densifier round-trips correctly, ~50% of per-combo
strategy rows compressed to a single `int`.

**SLURM submission fixes** (Stanford SC `svl` partition):
- `--account=vision` (was `default`, not on `AllowAccounts`).
- `--qos=normal` (the partition's default `svl` QoS rejects 72-CPU / 128G
  requests with `PartitionConfig`).

**Cluster smoke test:** 100 flops × 3 matchups → all wrote valid `.jsonl.zst`
files, every `.meta` claims `schema=combo-v2`, Python decoder densifies them
cleanly.

**Full run kicked off:** `sbatch --array=0-19%10 … TIER=full,COMBO_DATA=1`,
auto-chained after smoke via `--dependency=afterok:`. First 6 array tasks
running across svl5/12/15/16/17/18; ~6–8 h projected wallclock. Output:
`data/solves_combo/`.

**Known follow-ups** before/after the run completes:
- `verify_dataset` still parses v1 (dense) only — needs updating for `combo-v2`.
- `COMPRESS_THRESHOLD_GB` in `dataset_driver.rs:279` is still a hardcoded
  18.0; on svl's 250–510 GB nodes it should be raised so compression never
  triggers.
- Decoder script only takes a single file at a time; trivial to extend for
  batch audits.

## License

Copyright (C) 2022 Wataru Inariba

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License along with this program.  If not, see <https://www.gnu.org/licenses/>.
