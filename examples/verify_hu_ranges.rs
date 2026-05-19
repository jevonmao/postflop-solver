// Verify every non-empty range in `data/hu_200bb_ranges.txt` parses, and
// report per-range combo count / total weight. Run after editing the template:
//   cargo run --release --example verify_hu_ranges

use postflop_solver::hu_200bb_ranges::{Action, PreflopRanges};

fn main() {
    let ranges = match PreflopRanges::load_default() {
        Ok(r) => r,
        Err(e) => {
            eprintln!("ERROR loading template: {e}");
            std::process::exit(1);
        }
    };

    println!("{:<22} {:<6} {:>8} {:>12} {:>9}",
             "spot", "action", "combos", "total wt", "% of 1326");
    let actions = [Action::All, Action::Raise, Action::Call, Action::Fold];
    for (spot_name, spot) in &ranges.spots {
        let mut printed_any = false;
        for a in actions {
            if let Some(r) = spot.get(a) {
                let raw = r.raw_data();
                let (combos, total) = raw.iter().fold((0usize, 0.0f64), |(c, t), &w| {
                    if w > 0.0 { (c + 1, t + w as f64) } else { (c, t) }
                });
                let pct = 100.0 * combos as f64 / 1326.0;
                println!("{:<22} {:<6?} {:>8} {:>12.2} {:>8.2}%",
                         spot_name, a, combos, total, pct);
                printed_any = true;
            }
        }
        if !printed_any {
            println!("{:<22} {:<6} {:>8} {:>12} {:>9}    (all actions empty)",
                     spot_name, "-", "-", "-", "-");
        }
    }
}
