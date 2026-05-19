// SRP 200 BB solve using the *real* preflop ranges from
// `data/hu_200bb_ranges.txt`. End-to-end demo that:
//   1. Loads ranges from the template file
//   2. Builds a postflop tree with the recommended production config
//      (rich flop, lean turn/river, 2% pot exploit target)
//   3. Solves a flop
//   4. Reports range/nut advantage features at the root
//
// This is the smallest unit that proves the file → solver pipeline works.
// Use it as a template for the production driver.
//
// Run:
//   cargo run --release --example solve_srp_real_ranges
//   FLOP=Th9d8h cargo run --release --example solve_srp_real_ranges

use postflop_solver::hu_200bb_ranges::{Action, PreflopRanges};
use postflop_solver::*;

fn main() {
    // ---------- ranges ----------
    let ranges = PreflopRanges::load_default()
        .unwrap_or_else(|e| panic!("failed to load preflop ranges: {e}"));

    // IP = SB's raise range (combos weighted by raise frequency)
    let ip_range = ranges
        .get("SB_FIRST_ACTION", Action::Raise)
        .expect("SB_FIRST_ACTION/Raise must be present in template")
        .clone();

    // OOP = BB's call range vs SB raise
    let oop_range = ranges
        .get("BB_VS_SB_RAISE", Action::Call)
        .expect("BB_VS_SB_RAISE/Call must be present in template")
        .clone();

    let flop = std::env::var("FLOP").unwrap_or_else(|_| "Kh7d2c".to_string());
    println!("=== SRP 200 BB on {flop} ===");
    print_range_summary("BB (OOP, call vs SB raise)", &oop_range);
    print_range_summary("SB (IP,  raise)            ", &ip_range);

    // ---------- tree config (production recommendation) ----------
    // Chip units: 1 chip = 0.01 BB.
    // SB raises 2.5 BB → 250, BB calls. Pot = 500. Eff stack = 19,750.
    let card_config = CardConfig {
        range: [oop_range, ip_range],
        flop:  flop_from_str(&flop).unwrap(),
        turn:  NOT_DEALT,
        river: NOT_DEALT,
    };
    let flop_b  = BetSizeOptions::try_from(("33%,75%", "3x")).unwrap();
    let turn_b  = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
    let river_b = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
    let tree_config = TreeConfig {
        initial_state: BoardState::Flop,
        starting_pot: 500,
        effective_stack: 19_750,
        rake_rate: 0.0,
        rake_cap: 0.0,
        flop_bet_sizes:  [flop_b.clone(),  flop_b],
        turn_bet_sizes:  [turn_b.clone(),  turn_b],
        river_bet_sizes: [river_b.clone(), river_b],
        turn_donk_sizes: None,
        river_donk_sizes: None,
        add_allin_threshold: 1.5,
        force_allin_threshold: 0.15,
        merging_threshold: 0.1,
    };

    let tree = ActionTree::new(tree_config).unwrap();
    let mut game = PostFlopGame::with_config(card_config, tree)
        .expect("PostFlopGame construction failed — likely a range / board issue");

    let (mem, _) = game.memory_usage();
    let mem_gb = mem as f64 / (1u64 << 30) as f64;
    let use_compress = mem_gb > 18.0;
    println!("\nMemory: {:.2} GB ({})", mem_gb,
             if use_compress { "compressed" } else { "uncompressed" });
    game.allocate_memory(use_compress);

    // ---------- solve ----------
    let pot = game.tree_config().starting_pot as f32;
    let target = pot * 0.02; // 2% of pot
    let max_iter = 200;
    println!("Solving up to {max_iter} iterations, target = 2% of pot…");
    let t0 = std::time::Instant::now();
    let expl = solve(&mut game, max_iter, target, false);
    let solve_s = t0.elapsed().as_secs_f64();
    println!("Solved in {solve_s:.1}s — exploitability {:.3}% of pot",
             100.0 * expl / pot);

    // ---------- report root features ----------
    game.cache_normalized_weights();
    let oop_stats = range_stats(&game.equity(0), game.normalized_weights(0));
    let ip_stats  = range_stats(&game.equity(1), game.normalized_weights(1));
    let range_adv = if oop_stats.range_eq - ip_stats.range_eq >  0.04 { "OOP" }
                    else if ip_stats.range_eq - oop_stats.range_eq >  0.04 { "IP" }
                    else { "EVEN" };
    let nut_adv = if oop_stats.nut - ip_stats.nut >  0.03 { "OOP" }
                  else if ip_stats.nut - oop_stats.nut >  0.03 { "IP" }
                  else { "EVEN" };

    println!("\n=== Root node ({flop}) ===");
    println!("  BB (OOP):  eq {:.2}%  nut {:.2}%", 100.0 * oop_stats.range_eq, 100.0 * oop_stats.nut);
    println!("  SB (IP):   eq {:.2}%  nut {:.2}%", 100.0 * ip_stats.range_eq,  100.0 * ip_stats.nut);
    println!("  range advantage: {range_adv}");
    println!("  nut   advantage: {nut_adv}");

    // OOP's root strategy (range-averaged)
    let actions = game.available_actions();
    let n = game.private_cards(0).len();
    let strat = game.strategy();
    let w = game.normalized_weights(0);
    let mut freqs = vec![0.0f32; actions.len()];
    let mut wsum = 0.0f32;
    for i in 0..n {
        wsum += w[i];
        for (a, f) in freqs.iter_mut().enumerate() {
            *f += w[i] * strat[i + a * n];
        }
    }
    print!("  BB root strategy: ");
    for (a_idx, action) in actions.iter().enumerate() {
        print!("{:?}={:.1}%  ", action, 100.0 * freqs[a_idx] / wsum.max(1e-9));
    }
    println!();
}

fn print_range_summary(label: &str, range: &Range) {
    let raw = range.raw_data();
    let (combos, total) = raw.iter().fold((0usize, 0.0f64), |(c, t), &w| {
        if w > 0.0 { (c + 1, t + w as f64) } else { (c, t) }
    });
    println!("  {label}: {combos:>4} combos, total weight {total:.2} ({:.2}%)",
             100.0 * total / 1326.0);
}

struct RangeStats {
    range_eq: f32,
    nut:      f32,
}

fn range_stats(eq: &[f32], w: &[f32]) -> RangeStats {
    let total_w: f32 = w.iter().sum();
    let mut range_eq = 0.0;
    let mut nut = 0.0;
    for (&e, &wi) in eq.iter().zip(w.iter()) {
        range_eq += wi * e;
        if e > 0.85 { nut += wi; }
    }
    let norm = total_w.max(1e-9);
    RangeStats { range_eq: range_eq / norm, nut: nut / norm }
}
