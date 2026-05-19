// Audit a generated dataset under `data/solves/`. Reports per-matchup
// coverage, per-spot stats summarised across all solved files, and a sanity
// check on the first record of one randomly-chosen file per matchup.
//
// Use after running the driver, e.g.:
//   TIER=smoke cargo run --release --example dataset_driver
//   cargo run --release --example verify_dataset
//
// Env vars:
//   OUT_DIR=path   - dataset root (default: data/solves)
//   SAMPLE_LINES=N - number of records to spot-check per matchup (default: 1)

use postflop_solver::canonical::{canonical_flops_stratified, TIER_FULL, TIER_MEDIUM, TIER_SMOKE};

use std::collections::BTreeMap;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

const DEFAULT_OUT_DIR: &str = "data/solves";

#[derive(Default)]
struct MatchupStats {
    n_spots:     usize,
    n_records:   usize,
    sum_solve_s: f64,
    sum_build_s: f64,
    sum_walk_s:  f64,
    sum_mem_gb:  f64,
    sum_exploit: f64,
    max_solve_s: f64,
    max_mem_gb:  f64,
    compressed_count: usize,
    flop_canon_idxs: Vec<usize>,
}

fn main() {
    let out_dir = PathBuf::from(
        std::env::var("OUT_DIR").unwrap_or_else(|_| DEFAULT_OUT_DIR.into())
    );
    let sample_lines: usize = std::env::var("SAMPLE_LINES").ok()
        .and_then(|s| s.parse().ok()).unwrap_or(1);

    if !out_dir.exists() {
        eprintln!("ERROR: {} does not exist", out_dir.display());
        std::process::exit(1);
    }

    println!("Auditing {}\n", out_dir.display());

    let matchups = ["SRP", "3BP", "4BP"];
    let mut totals: BTreeMap<&str, MatchupStats> = BTreeMap::new();

    for m in &matchups {
        let mdir = out_dir.join(m);
        if !mdir.exists() { continue; }
        let stats = audit_matchup(&mdir);
        totals.insert(*m, stats);
    }

    if totals.is_empty() {
        println!("No matchup subdirectories found under {}.", out_dir.display());
        return;
    }

    // ---- Per-matchup summary ----
    println!("{:<6} {:>8} {:>9} {:>14} {:>10} {:>10} {:>10} {:>9} {:>10}",
             "match", "spots", "records", "avg_records", "avg_solve",
             "max_solve", "avg_mem", "compr", "avg_expl%");
    for (label, s) in &totals {
        if s.n_spots == 0 {
            println!("{:<6} {:>8} (no .meta files found)", label, 0);
            continue;
        }
        let n = s.n_spots as f64;
        // sum_exploit is already stored as a percentage of pot in the .meta
        // files; just average and print directly.
        println!("{:<6} {:>8} {:>9} {:>14.0} {:>9.1}s {:>9.1}s {:>9.2}GB {:>8}% {:>9.3}%",
                 label, s.n_spots, s.n_records, s.n_records as f64 / n,
                 s.sum_solve_s / n, s.max_solve_s, s.sum_mem_gb / n,
                 (100 * s.compressed_count / s.n_spots),
                 s.sum_exploit / n);
    }

    // ---- Tier-coverage check ----
    println!("\nTier coverage (canonical-index-based):");
    let strat = canonical_flops_stratified();
    let tier_canon: Vec<usize> = strat.iter().map(|(idx, _)| *idx).collect();
    let smoke_set:  std::collections::BTreeSet<_> = tier_canon[..TIER_SMOKE].iter().copied().collect();
    let medium_set: std::collections::BTreeSet<_> = tier_canon[..TIER_MEDIUM].iter().copied().collect();
    let full_set:   std::collections::BTreeSet<_> = tier_canon[..TIER_FULL].iter().copied().collect();
    for (label, s) in &totals {
        let done: std::collections::BTreeSet<_> = s.flop_canon_idxs.iter().copied().collect();
        let in_smoke  = done.intersection(&smoke_set ).count();
        let in_medium = done.intersection(&medium_set).count();
        let in_full   = done.intersection(&full_set  ).count();
        println!("  {label}: smoke {}/100  medium {}/500  full {}/1755",
                 in_smoke, in_medium, in_full);
    }

    // ---- Spot-check first record of N files per matchup ----
    if sample_lines > 0 {
        println!("\nSample record spot-check (first record of first {sample_lines} file(s) per matchup):");
        for label in matchups.iter() {
            let mdir = out_dir.join(label);
            if !mdir.exists() { continue; }
            let mut jsonls: Vec<_> = fs::read_dir(&mdir).unwrap()
                .filter_map(|e| e.ok().map(|e| e.path()))
                .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("jsonl"))
                .collect();
            jsonls.sort();
            for p in jsonls.iter().take(sample_lines) {
                let f = fs::File::open(p).unwrap();
                let mut br = BufReader::new(f);
                let mut first = String::new();
                let n = br.read_line(&mut first).unwrap_or(0);
                if n == 0 {
                    println!("  {label} {}: EMPTY", p.file_name().unwrap().to_string_lossy());
                    continue;
                }
                // Spot checks: contains the expected keys
                let ok = first.contains("\"matchup\":\"") &&
                         first.contains("\"board\":[") &&
                         first.contains("\"range_strategy\":[") &&
                         first.contains("\"range_advantage\":");
                println!("  {label} {}: {} ({} bytes for line 1)",
                         p.file_name().unwrap().to_string_lossy(),
                         if ok { "OK" } else { "MISSING_KEYS" },
                         first.trim_end().len());
                if !ok {
                    eprintln!("  ⚠ first record looked off:\n     {}", first.trim_end());
                }
            }
        }
    }
}

fn audit_matchup(mdir: &Path) -> MatchupStats {
    let mut s = MatchupStats::default();
    let Ok(entries) = fs::read_dir(mdir) else { return s; };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("meta") { continue; }
        let Ok(text) = fs::read_to_string(&path) else { continue; };
        let mut meta: BTreeMap<&str, &str> = BTreeMap::new();
        for line in text.lines() {
            if let Some((k, v)) = line.split_once('=') { meta.insert(k.trim(), v.trim()); }
        }
        s.n_spots += 1;
        s.n_records   += meta.get("n_records").and_then(|v| v.parse::<usize>().ok()).unwrap_or(0);
        let solve_s    = meta.get("solve_s").and_then(|v| v.parse::<f64>().ok()).unwrap_or(0.0);
        let build_s    = meta.get("build_s").and_then(|v| v.parse::<f64>().ok()).unwrap_or(0.0);
        let walk_s     = meta.get("walk_s") .and_then(|v| v.parse::<f64>().ok()).unwrap_or(0.0);
        let mem_gb     = meta.get("memory_gb").and_then(|v| v.parse::<f64>().ok()).unwrap_or(0.0);
        let exploit    = meta.get("exploitability_pct_pot").and_then(|v| v.parse::<f64>().ok()).unwrap_or(0.0);
        let compressed = meta.get("compressed").map(|v| *v == "true").unwrap_or(false);
        let flop_idx   = meta.get("flop_idx").and_then(|v| v.parse::<usize>().ok());

        s.sum_solve_s += solve_s;
        s.sum_build_s += build_s;
        s.sum_walk_s  += walk_s;
        s.sum_mem_gb  += mem_gb;
        s.sum_exploit += exploit;
        if solve_s > s.max_solve_s { s.max_solve_s = solve_s; }
        if mem_gb  > s.max_mem_gb  { s.max_mem_gb  = mem_gb;  }
        if compressed { s.compressed_count += 1; }
        if let Some(idx) = flop_idx { s.flop_canon_idxs.push(idx); }
    }
    s
}
