// Generate the 1,755 canonical flop equivalence classes and write them to
// `data/canonical_flops.txt`. One flop per line, three space-separated cards.
//
// Run once after cloning the repo:
//   cargo run --release --example canonical_flops
// Subsequent runs verify the file is up-to-date (rewrites if missing/wrong).

use postflop_solver::canonical::{canonical_flops, canonical_flops_stratified};
use postflop_solver::card_to_string;
use std::fs;
use std::io::Write;

const SORTED_PATH:     &str = "data/canonical_flops.txt";
const STRATIFIED_PATH: &str = "data/canonical_flops_stratified.txt";

fn main() {
    let flops = canonical_flops();
    assert_eq!(flops.len(), 1755, "canonical count must be 1755");

    // --- Sorted (canonical) order ---
    write_sorted(&flops);

    // --- Stratified order (used by the driver for tiered runs) ---
    write_stratified();

    // Sanity print
    let (mut three_distinct, mut paired, mut trips) = (0u32, 0u32, 0u32);
    for f in &flops {
        let mut r = [f[0] >> 2, f[1] >> 2, f[2] >> 2];
        r.sort_unstable();
        if r[0] == r[1] && r[1] == r[2] { trips += 1; }
        else if r[0] == r[1] || r[1] == r[2] { paired += 1; }
        else { three_distinct += 1; }
    }
    println!("Wrote {} flops to {} (sorted) and {} (stratified)",
             flops.len(), SORTED_PATH, STRATIFIED_PATH);
    println!("Breakdown:");
    println!("  three distinct ranks: {three_distinct}   (expected 1430)");
    println!("  paired + kicker:      {paired}   (expected 312)");
    println!("  trips:                {trips}    (expected 13)");

    let strat = canonical_flops_stratified();
    println!("\nFirst 10 in stratified order (smoke tier prefix):");
    for (idx, f) in strat.iter().take(10) {
        println!("  [{:>4}] {} {} {}", idx,
                 card_to_string(f[0]).unwrap(),
                 card_to_string(f[1]).unwrap(),
                 card_to_string(f[2]).unwrap());
    }
}

fn write_sorted(flops: &[[u8; 3]]) {
    let path = std::path::Path::new(SORTED_PATH);
    if let Some(parent) = path.parent() { fs::create_dir_all(parent).expect("mkdir"); }
    let tmp = path.with_extension("txt.tmp");
    {
        let mut f = fs::File::create(&tmp).expect("open tmp");
        writeln!(f, "# 1755 canonical flop equivalence classes for HU 200 BB.").unwrap();
        writeln!(f, "# Format: three space-separated cards per line, in canonical sort order.").unwrap();
        for flop in flops {
            writeln!(f, "{} {} {}",
                     card_to_string(flop[0]).unwrap(),
                     card_to_string(flop[1]).unwrap(),
                     card_to_string(flop[2]).unwrap()).unwrap();
        }
    }
    fs::rename(&tmp, path).expect("rename");
}

fn write_stratified() {
    let strat = canonical_flops_stratified();
    let path = std::path::Path::new(STRATIFIED_PATH);
    let tmp = path.with_extension("txt.tmp");
    {
        let mut f = fs::File::create(&tmp).expect("open tmp");
        writeln!(f, "# 1755 canonical flops in STRATIFIED order.").unwrap();
        writeln!(f, "# Any prefix is maximally representative — first 100 = smoke tier,").unwrap();
        writeln!(f, "# first 500 = medium tier, full 1755 = full tier.").unwrap();
        writeln!(f, "# Format: <canonical_sort_idx> <c1> <c2> <c3>. Filenames in data/solves/").unwrap();
        writeln!(f, "# use the canonical_sort_idx so they are stable across tiers.").unwrap();
        for (canon_idx, flop) in &strat {
            writeln!(f, "{:>4} {} {} {}",
                     canon_idx,
                     card_to_string(flop[0]).unwrap(),
                     card_to_string(flop[1]).unwrap(),
                     card_to_string(flop[2]).unwrap()).unwrap();
        }
    }
    fs::rename(&tmp, path).expect("rename");
}
