// Benchmark: single HU 200BB limped-pot flop solve.
// A limped pot = SB completes the small blind, BB checks option.
//   postflop pot     = 2 BB   = 200 chips
//   effective stack  = 199 BB = 19,800 chips   (SPR ~99 — highest of any HU spot)
//   OOP = BB (checked option),  IP = SB (limped)
//
// Goal — measure per-spot solve time + memory for the limped pot so we can
// decide whether to wire a LIMP matchup into the dataset driver. Run as:
//   cargo run --release --example limp_pot_bench
//   FLOP=Th9d8h cargo run --release --example limp_pot_bench

use postflop_solver::hu_200bb_ranges::{Action as RangeAction, PreflopRanges};
use postflop_solver::*;
use std::time::Instant;

fn main() {
    println!("=== HU 200BB limped-pot single-flop benchmark ===\n");

    let r = PreflopRanges::load_default().expect("load preflop ranges");
    let g = |spot, action| r.get(spot, action)
        .unwrap_or_else(|| panic!("range [{spot}/{action:?}] missing from template"))
        .clone();

    // OOP = BB checks option vs limp; IP = SB limps (completes SB).
    let oop = g("BB_VS_SB_LIMP",   RangeAction::Call);
    let ip  = g("SB_FIRST_ACTION", RangeAction::Call);

    // Production config: rich flop, lean turn/river, 2% pot exploit target.
    let flop_sizes  = BetSizeOptions::try_from(("33%,75%", "3x")).unwrap();
    let turn_sizes  = BetSizeOptions::try_from(("75%",     "3x")).unwrap();
    let river_sizes = BetSizeOptions::try_from(("75%",     "3x")).unwrap();

    let flop = std::env::var("FLOP").unwrap_or_else(|_| "Kh7d2c".to_string());

    let pot: i32 = 200;          // 2 BB limped pot
    let eff_stack: i32 = 19_800; // 199 BB behind

    println!("flop            {flop}");
    println!("starting_pot    {pot} chips (2.00 BB)");
    println!("effective_stack {eff_stack} chips (198.00 BB)");
    println!("SPR             {:.1}", eff_stack as f64 / pot as f64);
    println!("OOP combos      {}", oop.raw_data().iter().filter(|&&w| w > 0.0).count());
    println!("IP  combos      {}\n", ip.raw_data().iter().filter(|&&w| w > 0.0).count());

    let card_config = CardConfig {
        range: [oop.clone(), ip.clone()],
        flop: flop_from_str(&flop).unwrap(),
        turn: NOT_DEALT,
        river: NOT_DEALT,
    };
    let tree_config = TreeConfig {
        initial_state: BoardState::Flop,
        starting_pot: pot,
        effective_stack: eff_stack,
        rake_rate: 0.0, rake_cap: 0.0,
        flop_bet_sizes:  [flop_sizes.clone(),  flop_sizes],
        turn_bet_sizes:  [turn_sizes.clone(),  turn_sizes],
        river_bet_sizes: [river_sizes.clone(), river_sizes],
        turn_donk_sizes: None, river_donk_sizes: None,
        add_allin_threshold: 1.5,
        force_allin_threshold: 0.15,
        merging_threshold: 0.1,
    };

    let t0 = Instant::now();
    let tree = ActionTree::new(tree_config).unwrap();
    let mut game = PostFlopGame::with_config(card_config, tree).unwrap();
    let (uncompressed, compressed) = game.memory_usage();
    let mem_gb = uncompressed as f64 / (1u64 << 30) as f64;
    let mem_gb_c = compressed as f64 / (1u64 << 30) as f64;
    let use_compress = mem_gb > 18.0;
    game.allocate_memory(use_compress);
    let build_s = t0.elapsed().as_secs_f64();

    let target = pot as f32 * 0.02; // 2% of pot
    let max_iter = 200;

    println!("memory          {mem_gb:.2} GB uncompressed / {mem_gb_c:.2} GB compressed");
    println!("compression     {}", if use_compress { "ON (>18 GB)" } else { "off" });
    println!("build           {build_s:.2} s");
    println!("solving (max_iter={max_iter}, target={:.1} chips = 2% pot)...", target);

    let t1 = Instant::now();
    let exploit = solve(&mut game, max_iter, target, true);
    let solve_s = t1.elapsed().as_secs_f64();

    println!("\n--- result ---");
    println!("solve time      {solve_s:.2} s");
    println!("exploitability  {:.4} chips ({:.4}% of pot)", exploit, 100.0 * exploit / pot as f32);
    println!("projected 1755  {:.1} h (single-shard, this matchup only)",
             solve_s * 1755.0 / 3600.0);
}
