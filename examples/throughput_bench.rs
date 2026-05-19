// Throughput benchmark for postflop-solver — estimates how many spots per hour
// you can solve on this hardware for LLM-training dataset generation.
//
// Three street types:
//   - River:  initial_state = River, both players already on the river. Fast (<1s).
//   - Turn:   initial_state = Turn,  turn + river to solve. Medium (seconds).
//   - Flop:   initial_state = Flop,  full game tree. Slow (tens of seconds — minutes).
//
// Each spot uses realistic-but-modest bet sizing (one bet + raise per street)
// to keep the tree size representative of what a dataset generator would use.
// More bet sizes = larger trees = longer solves. See the notes at the bottom.
//
// Stake unit: 1 chip = 0.01 BB (1 BB = 100 chips). 100BB stacks.

use postflop_solver::*;
use std::time::Instant;

#[derive(Default, Clone, Copy)]
struct SpotResult {
    label_idx: usize, // index into LABELS
    mem_gb:    f64,
    build_s:   f64,
    solve_s:   f64,
    iters:     u32,
    exploit_pct_pot: f32,
}

static LABELS: &[&str] = &[
    // rivers
    "RIVER  Kh7d2c-9s-3h  (dry)",
    "RIVER  QhJh9c-Th-8d  (wet/straight)",
    // turns
    "TURN   Kh7d2c-9s     (BTN-favored, brick)",
    "TURN   QhJh9c-Th     (wet — many draws)",
    // flop
    "FLOP   Kh7d2c        (dry, BTN-favored)",
    "FLOP   QhJh9c        (wet)",
];

fn main() {
    let mut results: Vec<SpotResult> = Vec::new();

    // ---------- Common ranges ----------
    // BTN open (IP), BB call (OOP), 100BB stacks, 5.5 BB pot to flop (550 chips)
    let bb_call = "TT-22,AJs-A2s,KJs-K2s,QJs-Q5s,J9s-J7s,T8s-T7s,97s-96s,87s-86s,76s-75s,65s,54s,\
                   AJo-A9o,KQo-KTo,QJo-QTo,JTo,T9o";
    let btn_open = "22+,A2s+,K2s+,Q4s+,J7s+,T7s+,97s+,86s+,75s+,64s+,54s,\
                    A2o+,K9o+,Q9o+,J9o+,T9o,98o";

    let small = BetSizeOptions::try_from(("50%", "3x")).unwrap(); // one size per street
    let big   = BetSizeOptions::try_from(("75%", "3x")).unwrap();

    // ---------- River spots ----------
    for (i, (flop, turn, river)) in [
        ("Kh7d2c", "9s", "3h"),
        ("QhJh9c", "Th", "8d"),
    ].iter().enumerate() {
        let r = solve_one(
            i,
            bb_call, btn_open,
            flop, *turn, *river,
            BoardState::River,
            &big, &big, &big,
            300,             // max iters
            0.003,           // target = 0.3% of pot
        );
        results.push(r);
        print_one(&r);
    }

    // ---------- Turn spots ----------
    for (i, (flop, turn)) in [
        ("Kh7d2c", "9s"),
        ("QhJh9c", "Th"),
    ].iter().enumerate() {
        let r = solve_one(
            2 + i,
            bb_call, btn_open,
            flop, *turn, "",
            BoardState::Turn,
            &small, &big, &big,
            300,
            0.005,           // 0.5% of pot
        );
        results.push(r);
        print_one(&r);
    }

    // ---------- Flop spots ----------
    for (i, flop) in ["Kh7d2c", "QhJh9c"].iter().enumerate() {
        let r = solve_one(
            4 + i,
            bb_call, btn_open,
            flop, "", "",
            BoardState::Flop,
            &small, &big, &big,
            200,
            0.01,            // 1% of pot — looser for time
        );
        results.push(r);
        print_one(&r);
    }

    // ---------- Summary table ----------
    println!("\n================ Summary ================");
    println!("{:<40} {:>8} {:>8} {:>8} {:>8} {:>8}",
             "Spot", "mem(GB)", "build(s)", "solve(s)", "iters", "expl%");
    for r in &results {
        println!("{:<40} {:>8.3} {:>8.3} {:>8.3} {:>8} {:>8.3}",
                 LABELS[r.label_idx], r.mem_gb, r.build_s, r.solve_s, r.iters, r.exploit_pct_pot);
    }

    // ---------- Throughput projection ----------
    let mut by_street = [(0u32, 0.0f64); 3]; // (count, total seconds incl build+solve)
    for r in &results {
        let bucket = if r.label_idx < 2 { 0 } else if r.label_idx < 4 { 1 } else { 2 };
        by_street[bucket].0 += 1;
        by_street[bucket].1 += r.build_s + r.solve_s;
    }
    let names = ["River", "Turn", "Flop"];
    println!("\n================ Projected throughput ================");
    println!("{:<8} {:>10} {:>14} {:>14} {:>14}",
             "Street", "avg sec", "spots/hour", "spots/day", "spots/week");
    for i in 0..3 {
        let (n, t) = by_street[i];
        if n == 0 { continue; }
        let avg = t / n as f64;
        let per_hour = 3600.0 / avg;
        println!("{:<8} {:>10.2} {:>14.0} {:>14.0} {:>14.0}",
                 names[i], avg, per_hour, per_hour * 24.0, per_hour * 24.0 * 7.0);
    }
}

#[allow(clippy::too_many_arguments)]
fn solve_one(
    label_idx: usize,
    oop_range: &str,
    ip_range: &str,
    flop: &str,
    turn: &str,
    river: &str,
    initial: BoardState,
    flop_b: &BetSizeOptions,
    turn_b: &BetSizeOptions,
    river_b: &BetSizeOptions,
    max_iter: u32,
    target_pct: f32,
) -> SpotResult {
    let card_config = CardConfig {
        range: [oop_range.parse().unwrap(), ip_range.parse().unwrap()],
        flop:  flop_from_str(flop).unwrap(),
        turn:  if turn.is_empty()  { NOT_DEALT } else { card_from_str(turn ).unwrap() },
        river: if river.is_empty() { NOT_DEALT } else { card_from_str(river).unwrap() },
    };
    let tree_config = TreeConfig {
        initial_state: initial,
        starting_pot: 550,
        effective_stack: 9750,
        rake_rate: 0.0,
        rake_cap: 0.0,
        flop_bet_sizes:  [flop_b.clone(),  flop_b.clone()],
        turn_bet_sizes:  [turn_b.clone(),  turn_b.clone()],
        river_bet_sizes: [river_b.clone(), river_b.clone()],
        turn_donk_sizes: None,
        river_donk_sizes: None,
        add_allin_threshold: 1.5,
        force_allin_threshold: 0.15,
        merging_threshold: 0.1,
    };

    let t0 = Instant::now();
    let tree = ActionTree::new(tree_config).unwrap();
    let mut game = PostFlopGame::with_config(card_config, tree).unwrap();
    let mem_gb = game.memory_usage().0 as f64 / (1u64 << 30) as f64;
    game.allocate_memory(false);
    let build_s = t0.elapsed().as_secs_f64();

    let target = game.tree_config().starting_pot as f32 * target_pct;
    let t1 = Instant::now();
    let exploit = solve(&mut game, max_iter, target, false /* no progress printing */);
    let solve_s = t1.elapsed().as_secs_f64();

    let pot = game.tree_config().starting_pot as f32;
    SpotResult {
        label_idx,
        mem_gb,
        build_s,
        solve_s,
        iters: max_iter, // solver may have early-stopped; we just report cap as upper bound
        exploit_pct_pot: 100.0 * exploit / pot,
    }
}

fn print_one(r: &SpotResult) {
    println!("[done] {:<40}  mem {:>6.3}GB  build {:>7.3}s  solve {:>8.4}s  expl {:>6.3}% of pot",
             LABELS[r.label_idx], r.mem_gb, r.build_s, r.solve_s, r.exploit_pct_pot);
}
