// Production driver — iterates over (matchup × canonical_flop) and writes one
// `.jsonl` of decision-node records per spot. Resumable via output-file presence.
//
// Input files:
//   - data/hu_200bb_ranges.txt    (preflop ranges; required)
//   - data/canonical_flops.txt    (1755 canonical flops; required)
//
// Output layout:
//   data/solves/<matchup>/<idx>_<flop>.jsonl       e.g. 0001_2c2d2h.jsonl
//   data/solves/<matchup>/<idx>_<flop>.meta        small text file with solve stats
//
// Run:
//   cargo run --release --example dataset_driver
//
// Flop ordering: STRATIFIED. Any prefix of N flops is maximally representative
// (covers all rank-shape × suit-pattern × high-card buckets) so smaller tiers
// can be solved first and serve as smoke-test datasets.
//
// Filenames use the **stable canonical sort index** (1..1755), not the
// stratified-order position, so re-running with a different tier never causes
// file-name churn or recomputation.
//
// Useful env vars:
//   TIER=smoke|medium|full - 100 / 500 / 1755 flops (default: full)
//   MATCHUPS=SRP,3BP,4BP   - subset of matchups to run (default: all three)
//   FLOP_LIMIT=N           - explicit cap (overrides TIER if set)
//   FLOP_START=N           - skip first N positions in stratified order
//   FROM_INDEX=N           - alias for FLOP_START
//   SHARD_INDEX=I          - this worker's index in [0, SHARD_COUNT)
//   SHARD_COUNT=N          - total # of workers cooperating (default: 1)
//                            Workers stripe through stratified order:
//                            worker I takes positions I, I+N, I+2N, …
//                            All workers write to the same OUT_DIR and rely
//                            on file-presence to avoid duplicating work.
//   MAX_ITER=N             - solver iteration cap (default: 200)
//   TARGET_PCT=F           - exploitability target (default: 0.02 = 2%)
//   TURN_SAMPLES=N         - chance sampling for turn (default: 8)
//   RIVER_SAMPLES=N        - chance sampling for river (default: 6)
//   OUT_DIR=path           - override `data/solves`
//
// To resume after a crash, just re-run; per-spot files are written atomically.

use postflop_solver::canonical::{canonical_flops_stratified, TIER_FULL, TIER_MEDIUM, TIER_SMOKE};
#[allow(unused_imports)]
use postflop_solver::dataset_walker::{header_json, record_to_json, walk, NodeRecord, WalkConfig};
use postflop_solver::hu_200bb_ranges::{Action as RangeAction, PreflopRanges};
use postflop_solver::*;

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Instant;

#[derive(Clone, Copy)]
struct Matchup {
    label:           &'static str,
    oop_spot:        &'static str,
    oop_action:      RangeAction,
    ip_spot:         &'static str,
    ip_action:       RangeAction,
    starting_pot:    i32,
    effective_stack: i32,
}

const MATCHUPS: &[Matchup] = &[
    Matchup {
        label: "SRP",
        oop_spot: "BB_VS_SB_RAISE", oop_action: RangeAction::Call,
        ip_spot:  "SB_FIRST_ACTION", ip_action: RangeAction::Raise,
        starting_pot: 500, effective_stack: 19_750,
    },
    Matchup {
        label: "3BP",
        oop_spot: "BB_VS_SB_RAISE", oop_action: RangeAction::Raise,
        ip_spot:  "SB_VS_BB_3BET",  ip_action:  RangeAction::Call,
        starting_pot: 2_000, effective_stack: 19_000,
    },
    Matchup {
        label: "4BP",
        oop_spot: "BB_VS_SB_4BET",  oop_action: RangeAction::Call,
        ip_spot:  "SB_VS_BB_3BET",  ip_action:  RangeAction::Raise,
        starting_pot: 5_000, effective_stack: 17_500,
    },
];

fn env_usize(key: &str, default: usize) -> usize {
    std::env::var(key).ok().and_then(|s| s.parse().ok()).unwrap_or(default)
}
fn env_f32(key: &str, default: f32) -> f32 {
    std::env::var(key).ok().and_then(|s| s.parse().ok()).unwrap_or(default)
}

fn main() {
    // ---------- config ----------
    let max_iter:      u32     = env_usize("MAX_ITER", 200) as u32;
    let target_pct:    f32     = env_f32("TARGET_PCT", 0.02);
    let turn_samples:  usize   = env_usize("TURN_SAMPLES", 8);
    let river_samples: usize   = env_usize("RIVER_SAMPLES", 6);
    let emit_combo_data: bool  = std::env::var("COMBO_DATA").map(|v| v == "1" || v.to_lowercase() == "true").unwrap_or(false);
    let tier_default = match std::env::var("TIER").ok().map(|s| s.to_lowercase()).as_deref() {
        Some("smoke")  => TIER_SMOKE,
        Some("medium") => TIER_MEDIUM,
        Some("full")   => TIER_FULL,
        _              => TIER_FULL,
    };
    let flop_limit:    usize   = env_usize("FLOP_LIMIT", tier_default);
    let flop_start:    usize   = std::env::var("FLOP_START").ok()
        .or_else(|| std::env::var("FROM_INDEX").ok())
        .and_then(|s| s.parse().ok()).unwrap_or(0);
    let shard_index:   usize   = env_usize("SHARD_INDEX", 0);
    let shard_count:   usize   = env_usize("SHARD_COUNT", 1).max(1);
    assert!(shard_index < shard_count,
        "SHARD_INDEX ({shard_index}) must be < SHARD_COUNT ({shard_count})");
    let out_dir: PathBuf       = std::env::var("OUT_DIR").map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("data/solves"));

    let selected_matchups: Vec<&Matchup> = match std::env::var("MATCHUPS") {
        Ok(s) => {
            let want: Vec<String> = s.split(',').map(|x| x.trim().to_uppercase()).collect();
            MATCHUPS.iter().filter(|m| want.iter().any(|w| w == m.label)).collect()
        }
        Err(_) => MATCHUPS.iter().collect(),
    };

    let ranges = PreflopRanges::load_default().expect("load preflop ranges");
    let flops_strat = canonical_flops_stratified();  // Vec<(canonical_idx_1based, [Card; 3])>
    assert_eq!(flops_strat.len(), 1755);
    let window_end = (flop_start + flop_limit).min(flops_strat.len());
    let flops_window_full = &flops_strat[flop_start.min(flops_strat.len())..window_end];
    // Apply sharding: this worker takes positions {SHARD_INDEX, SHARD_INDEX+SHARD_COUNT, …}.
    let flops_window: Vec<&(usize, [u8; 3])> = flops_window_full.iter()
        .enumerate()
        .filter(|(i, _)| i % shard_count == shard_index)
        .map(|(_, x)| x)
        .collect();

    let tier_label = match flop_limit {
        TIER_SMOKE  => " (smoke)",
        TIER_MEDIUM => " (medium)",
        TIER_FULL   => " (full)",
        _           => "",
    };

    println!("=== HU 200 BB dataset driver ===");
    println!("Matchups:       {}", selected_matchups.iter().map(|m| m.label).collect::<Vec<_>>().join(", "));
    println!("Flops window:   stratified[{}..{}) of 1755{}   ({} flops in window)",
             flop_start, window_end, tier_label, flops_window_full.len());
    if shard_count > 1 {
        println!("Shard:          {} of {}   ({} flops in this shard)",
                 shard_index, shard_count, flops_window.len());
    }
    println!("Iter cap:       {max_iter}");
    println!("Target:         {:.1}% of pot", 100.0 * target_pct);
    println!("Chance samples: {} turn × {} river per flop", turn_samples, river_samples);
    println!("Combo data:     {}", if emit_combo_data { "enabled (COMBO_DATA=1)" } else { "disabled" });
    println!("Output:         {}\n", out_dir.display());

    // Build the OOP × IP range pair per matchup (clone once, reuse per flop).
    let pre: Vec<(Matchup, Range, Range)> = selected_matchups.iter().map(|m| {
        let oop = ranges.get(m.oop_spot, m.oop_action)
            .unwrap_or_else(|| panic!("missing [{}/{:?}] in ranges file", m.oop_spot, m.oop_action))
            .clone();
        let ip = ranges.get(m.ip_spot, m.ip_action)
            .unwrap_or_else(|| panic!("missing [{}/{:?}] in ranges file", m.ip_spot, m.ip_action))
            .clone();
        (**m, oop, ip)
    }).collect();

    let flop_b  = BetSizeOptions::try_from(("33%,75%", "3x")).unwrap();
    let turn_b  = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
    let river_b = BetSizeOptions::try_from(("75%",     "3x")).unwrap();

    let walk_cfg = WalkConfig {
        n_turn_samples: turn_samples,
        n_river_samples: river_samples,
        record_limit: 1_000_000,
        emit_combo_data,
    };

    // ---------- driver loop ----------
    let total_spots = pre.len() * flops_window.len();
    let started = Instant::now();
    let mut done_this_run = 0usize;
    let mut skipped = 0usize;
    let mut failed = 0usize;

    for (mi, (matchup, oop_range, ip_range)) in pre.iter().enumerate() {
        let m_dir = out_dir.join(matchup.label);
        fs::create_dir_all(&m_dir).expect("create matchup dir");

        for (fi, pair) in flops_window.iter().enumerate() {
            let (canon_idx, flop_cards) = **pair;
            let flop_idx_global = canon_idx; // stable across tiers
            let flop_str = flop_to_string(&flop_cards);
            let stem = format!("{:04}_{}", flop_idx_global, flop_str);
            let ext  = if emit_combo_data { "jsonl.zst" } else { "jsonl" };
            let out_path  = m_dir.join(format!("{stem}.{ext}"));
            let meta_path = m_dir.join(format!("{stem}.meta"));

            let progress_idx = mi * flops_window.len() + fi + 1;

            if out_path.exists() && meta_path.exists() {
                let skip = if emit_combo_data {
                    // Re-solve if this spot was written without combo data
                    // or with the old (v1, dense) combo schema.
                    fs::read_to_string(&meta_path)
                        .map(|s| s.contains("schema=combo-v2"))
                        .unwrap_or(false)
                } else {
                    true
                };
                if skip { skipped += 1; continue; }
            }

            let result = solve_and_write(
                matchup, flop_idx_global as u32, flop_cards,
                oop_range, ip_range,
                &flop_b, &turn_b, &river_b,
                max_iter, target_pct, walk_cfg,
                &out_path, &meta_path,
                emit_combo_data,
            );

            let elapsed = started.elapsed().as_secs_f64();
            match result {
                Ok(stats) => {
                    done_this_run += 1;
                    let avg = elapsed / done_this_run as f64;
                    let remaining = total_spots.saturating_sub(progress_idx);
                    let eta_h = avg * remaining as f64 / 3600.0;
                    println!("[{progress_idx:>5}/{total_spots}] {} {flop_str}  mem={:.1}GB({}) build={:.1}s solve={:.1}s walk={:.1}s expl={:.2}%  records={}  ETA={:.1}h",
                             matchup.label,
                             stats.mem_gb, if stats.use_compress {"c"} else {"u"},
                             stats.build_s, stats.solve_s, stats.walk_s,
                             100.0 * stats.expl_pct, stats.n_records, eta_h);
                }
                Err(e) => {
                    failed += 1;
                    eprintln!("[{progress_idx:>5}/{total_spots}] {} {flop_str}  FAILED: {e}", matchup.label);
                }
            }
        }
    }

    let total_s = started.elapsed().as_secs_f64();
    println!("\nDone in {:.1} min.  new={done_this_run}  skipped={skipped}  failed={failed}",
             total_s / 60.0);
}

struct SolveStats {
    mem_gb:       f64,
    use_compress: bool,
    build_s:      f64,
    solve_s:      f64,
    walk_s:       f64,
    expl_pct:     f32,
    n_records:    usize,
}

#[allow(clippy::too_many_arguments)]
fn solve_and_write(
    matchup: &Matchup, flop_idx: u32, flop_cards: [u8; 3],
    oop_range: &Range, ip_range: &Range,
    flop_b: &BetSizeOptions, turn_b: &BetSizeOptions, river_b: &BetSizeOptions,
    max_iter: u32, target_pct: f32, walk_cfg: WalkConfig,
    out_path: &Path, meta_path: &Path,
    emit_combo_data: bool,
) -> Result<SolveStats, String> {
    let card_config = CardConfig {
        range: [oop_range.clone(), ip_range.clone()],
        flop: flop_cards,
        turn: NOT_DEALT,
        river: NOT_DEALT,
    };
    let tree_config = TreeConfig {
        initial_state: BoardState::Flop,
        starting_pot: matchup.starting_pot,
        effective_stack: matchup.effective_stack,
        rake_rate: 0.0, rake_cap: 0.0,
        flop_bet_sizes:  [flop_b.clone(),  flop_b.clone()],
        turn_bet_sizes:  [turn_b.clone(),  turn_b.clone()],
        river_bet_sizes: [river_b.clone(), river_b.clone()],
        turn_donk_sizes: None, river_donk_sizes: None,
        add_allin_threshold: 1.5,
        force_allin_threshold: 0.15,
        merging_threshold: 0.1,
    };

    let t_build = Instant::now();
    let tree = ActionTree::new(tree_config).map_err(|e| format!("action tree: {e}"))?;
    let mut game = PostFlopGame::with_config(card_config, tree)
        .map_err(|e| format!("PostFlopGame: {e}"))?;
    let mem_gb = game.memory_usage().0 as f64 / (1u64 << 30) as f64;
    let use_compress = mem_gb > 18.0;
    game.allocate_memory(use_compress);
    let build_s = t_build.elapsed().as_secs_f64();

    let pot = game.tree_config().starting_pot as f32;
    let t_solve = Instant::now();
    let expl = solve(&mut game, max_iter, pot * target_pct, false);
    let solve_s = t_solve.elapsed().as_secs_f64();

    // Snapshot combo enumeration for the header (constant across this spot's tree).
    let combos_oop: Vec<(u8, u8)> = game.private_cards(0).to_vec();
    let combos_ip:  Vec<(u8, u8)> = game.private_cards(1).to_vec();

    let t_walk = Instant::now();
    let mut records: Vec<NodeRecord> = Vec::new();
    walk(&mut game, matchup.label, flop_idx, walk_cfg, &mut records);
    let walk_s = t_walk.elapsed().as_secs_f64();

    // Write output atomically. When emitting combo data we compress with zstd
    // (the records grow ~40–100x without compression). Otherwise plain JSONL.
    let tmp = out_path.with_extension("tmp");
    if emit_combo_data {
        write_jsonl_zst(&tmp, matchup, flop_idx, &flop_cards,
                        &combos_oop, &combos_ip, &records)?;
    } else {
        let mut f = fs::File::create(&tmp).map_err(|e| format!("create tmp: {e}"))?;
        for r in &records {
            writeln!(f, "{}", record_to_json(r)).map_err(|e| format!("write: {e}"))?;
        }
        f.sync_all().map_err(|e| format!("fsync: {e}"))?;
    }
    fs::rename(&tmp, out_path).map_err(|e| format!("rename: {e}"))?;

    // Meta file (small text — solve stats).
    let schema = if emit_combo_data { "combo-v2" } else { "range-only" };
    let meta = format!(
        "matchup={}\nflop_idx={}\nschema={}\nmemory_gb={:.2}\ncompressed_solver={}\nbuild_s={:.2}\nsolve_s={:.2}\nwalk_s={:.2}\nexploitability_pct_pot={:.4}\nn_records={}\nmax_iter={}\ntarget_pct={:.4}\nturn_samples={}\nriver_samples={}\ncombo_data={}\nn_combos_oop={}\nn_combos_ip={}\n",
        matchup.label, flop_idx, schema, mem_gb, use_compress, build_s, solve_s, walk_s,
        100.0 * expl / pot, records.len(), max_iter, target_pct,
        walk_cfg.n_turn_samples, walk_cfg.n_river_samples,
        emit_combo_data, combos_oop.len(), combos_ip.len(),
    );
    fs::write(meta_path, meta).map_err(|e| format!("write meta: {e}"))?;

    Ok(SolveStats {
        mem_gb,
        use_compress,
        build_s,
        solve_s,
        walk_s,
        expl_pct: expl / pot,
        n_records: records.len(),
    })
}

#[cfg(feature = "zstd")]
fn write_jsonl_zst(
    tmp: &Path,
    matchup: &Matchup,
    flop_idx: u32,
    flop_cards: &[u8; 3],
    combos_oop: &[(u8, u8)],
    combos_ip:  &[(u8, u8)],
    records: &[NodeRecord],
) -> Result<(), String> {
    use std::io::BufWriter;
    let f = fs::File::create(tmp).map_err(|e| format!("create tmp: {e}"))?;
    let buf = BufWriter::new(f);
    let mut enc = zstd::stream::write::Encoder::new(buf, 6)
        .map_err(|e| format!("zstd encoder: {e}"))?;
    // long-range mode + multithread when available
    let _ = enc.multithread(num_cpus_for_zstd());
    // Header line (combo enumeration, schema marker).
    writeln!(
        enc,
        "{}",
        header_json(
            matchup.label, flop_idx, flop_cards,
            matchup.starting_pot, matchup.effective_stack,
            combos_oop, combos_ip,
        )
    ).map_err(|e| format!("header write: {e}"))?;
    for r in records {
        writeln!(enc, "{}", record_to_json(r)).map_err(|e| format!("write: {e}"))?;
    }
    let buf = enc.finish().map_err(|e| format!("zstd finish: {e}"))?;
    let f = buf.into_inner().map_err(|e| format!("flush: {e}"))?;
    f.sync_all().map_err(|e| format!("fsync: {e}"))?;
    Ok(())
}

#[cfg(not(feature = "zstd"))]
fn write_jsonl_zst(
    _tmp: &Path,
    _matchup: &Matchup,
    _flop_idx: u32,
    _flop_cards: &[u8; 3],
    _combos_oop: &[(u8, u8)],
    _combos_ip:  &[(u8, u8)],
    _records: &[NodeRecord],
) -> Result<(), String> {
    Err(
        "COMBO_DATA=1 requires the `zstd` feature. \
         Rebuild: `cargo build --release --features zstd --example dataset_driver`.".into()
    )
}

#[cfg(feature = "zstd")]
fn num_cpus_for_zstd() -> u32 {
    std::thread::available_parallelism().map(|n| n.get() as u32).unwrap_or(1)
}

fn flop_to_string(flop: &[u8; 3]) -> String {
    let mut s = String::with_capacity(6);
    for c in flop {
        s.push_str(&card_to_string(*c).unwrap_or_else(|_| "??".into()));
    }
    s
}
