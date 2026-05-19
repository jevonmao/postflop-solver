//! Canonical flop enumeration — the 1,755 strategically distinct flops.
//!
//! Strategies in NLHE are identical up to suit relabeling because preflop
//! ranges are suit-symmetric (`AKs` covers all four suits equivalently).
//! That collapses C(52,3) = 22,100 raw flops into 1,755 equivalence classes.
//!
//! Breakdown:
//!
//! | Rank shape                | Rank patterns | Suit patterns | Total |
//! |---------------------------|--------------:|--------------:|------:|
//! | Three distinct (e.g. K72) | C(13,3) = 286 | 5             | 1,430 |
//! | Pair + kicker (KK7)       | 13 × 12 = 156 | 2             |   312 |
//! | Trips (KKK)               | 13            | 1             |    13 |
//! | **Total**                 |               |               | **1,755** |
//!
//! Card encoding (matches postflop-solver convention): `card = (rank << 2) | suit`,
//! with `rank ∈ 0..13` (2 → A) and `suit ∈ 0..4` (c, d, h, s — alphabetical).

use crate::Card;

/// Compute the canonical form of a flop by taking the lexicographically
/// minimum over all 24 suit-label permutations of the sorted card vector.
///
/// Two flops have the same canonical form iff they are strategically
/// equivalent under suit-symmetric ranges.
pub fn canonical_flop(cards: [Card; 3]) -> [Card; 3] {
    let mut best: Option<[Card; 3]> = None;
    for perm in SUIT_PERMUTATIONS.iter() {
        let mut relabeled: [Card; 3] = [
            relabel_suit(cards[0], *perm),
            relabel_suit(cards[1], *perm),
            relabel_suit(cards[2], *perm),
        ];
        relabeled.sort_unstable();
        if best.map_or(true, |b| relabeled < b) {
            best = Some(relabeled);
        }
    }
    best.unwrap()
}

#[inline]
fn relabel_suit(card: Card, perm: [u8; 4]) -> Card {
    let rank = card >> 2;
    let suit = (card & 3) as usize;
    (rank << 2) | perm[suit]
}

/// Enumerate the 1,755 canonical flop equivalence classes. Each entry is a
/// sorted 3-card tuple in canonical (lexicographically-minimum) form.
///
/// Asserts `result.len() == 1755`; panics otherwise.
pub fn canonical_flops() -> Vec<[Card; 3]> {
    use std::collections::BTreeSet;
    let mut seen: BTreeSet<[Card; 3]> = BTreeSet::new();
    for a in 0..52u8 {
        for b in (a + 1)..52u8 {
            for c in (b + 1)..52u8 {
                seen.insert(canonical_flop([a, b, c]));
            }
        }
    }
    let out: Vec<_> = seen.into_iter().collect();
    debug_assert_eq!(out.len(), 1755,
        "canonical flop count is wrong — got {} expected 1755", out.len());
    out
}

/// Standard tier sizes for incremental dataset generation.
pub const TIER_SMOKE:  usize = 100;
pub const TIER_MEDIUM: usize = 500;
pub const TIER_FULL:   usize = 1755;

/// Return a stratified ordering of the given flops such that any prefix is
/// maximally representative of the full set.
///
/// The stratification key is `(rank_shape, suit_pattern, high_card_bucket)`:
/// - rank_shape ∈ {trips, paired, three-distinct}
/// - suit_pattern ∈ {monotone, two-tone, rainbow}
/// - high_card_bucket: A / K / Q-J / T-9 / 8-7 / 6-2  (6 buckets)
///
/// We bucket all flops by this key, then round-robin one flop from each bucket
/// in turn. The first prefix-N of the returned indices touches every
/// non-empty bucket at least once if N ≥ # of distinct strata (about 35).
pub fn stratified_order(flops: &[[Card; 3]]) -> Vec<usize> {
    use std::collections::BTreeMap;
    let mut buckets: BTreeMap<(u8, u8, u8), Vec<usize>> = BTreeMap::new();
    for (i, f) in flops.iter().enumerate() {
        buckets.entry(flop_stratum(*f)).or_default().push(i);
    }
    // Drain each bucket from front (oldest by canonical sort order).
    let mut bucket_vec: Vec<std::collections::VecDeque<usize>> =
        buckets.into_values().map(|v| v.into_iter().collect()).collect();
    let mut output = Vec::with_capacity(flops.len());
    loop {
        let mut emitted = false;
        for b in bucket_vec.iter_mut() {
            if let Some(idx) = b.pop_front() {
                output.push(idx);
                emitted = true;
            }
        }
        if !emitted { break; }
    }
    output
}

/// Combined: returns canonical flops in stratified order, each paired with
/// its **stable** sorted-canonical index (1-based) so output filenames remain
/// the same regardless of which tier is being run.
pub fn canonical_flops_stratified() -> Vec<(usize, [Card; 3])> {
    let flops = canonical_flops();
    let order = stratified_order(&flops);
    order.into_iter().map(|i| (i + 1, flops[i])).collect()
}

fn flop_stratum(f: [Card; 3]) -> (u8, u8, u8) {
    let (r0, r1, r2) = (f[0] >> 2, f[1] >> 2, f[2] >> 2);
    let (s0, s1, s2) = (f[0] & 3, f[1] & 3, f[2] & 3);

    let rank_shape = if r0 == r1 && r1 == r2 { 0 }
                     else if r0 == r1 || r1 == r2 || r0 == r2 { 1 }
                     else { 2 };
    let suit_pattern = if s0 == s1 && s1 == s2 { 0 }            // monotone
                       else if s0 == s1 || s1 == s2 || s0 == s2 { 1 }  // two-tone
                       else { 2 };                              // rainbow
    let high_card_bucket = match r2 {
        12       => 0, // A
        11       => 1, // K
        9..=10   => 2, // Q-J
        7..=8    => 3, // T-9
        5..=6    => 4, // 8-7
        _        => 5, // 6-2
    };
    (rank_shape, suit_pattern, high_card_bucket)
}

/// All 24 permutations of the 4 suit labels.
const SUIT_PERMUTATIONS: [[u8; 4]; 24] = [
    [0,1,2,3], [0,1,3,2], [0,2,1,3], [0,2,3,1], [0,3,1,2], [0,3,2,1],
    [1,0,2,3], [1,0,3,2], [1,2,0,3], [1,2,3,0], [1,3,0,2], [1,3,2,0],
    [2,0,1,3], [2,0,3,1], [2,1,0,3], [2,1,3,0], [2,3,0,1], [2,3,1,0],
    [3,0,1,2], [3,0,2,1], [3,1,0,2], [3,1,2,0], [3,2,0,1], [3,2,1,0],
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_form_is_idempotent() {
        // Applying canonicalization twice should be a no-op.
        let f1 = [0, 5, 10]; // arbitrary triple
        let c1 = canonical_flop(f1);
        let c2 = canonical_flop(c1);
        assert_eq!(c1, c2);
    }

    #[test]
    fn equivalent_flops_share_canonical_form() {
        // Kh7d2c and Ks7h2d are both rainbow K72 — different suits, same class.
        let a = [(0 << 2) | 0, (5 << 2) | 1, (11 << 2) | 2]; // 2c 7d Kh
        let b = [(0 << 2) | 1, (5 << 2) | 2, (11 << 2) | 3]; // 2d 7h Ks
        assert_eq!(canonical_flop(a), canonical_flop(b));
    }

    #[test]
    fn enumerator_produces_exactly_1755() {
        let flops = canonical_flops();
        assert_eq!(flops.len(), 1755);
    }

    #[test]
    fn stratified_order_is_full_permutation() {
        let flops = canonical_flops();
        let order = stratified_order(&flops);
        assert_eq!(order.len(), flops.len(), "stratified order must include every flop");
        let mut sorted = order.clone();
        sorted.sort_unstable();
        for (i, &v) in sorted.iter().enumerate() {
            assert_eq!(v, i, "stratified order must be a permutation of 0..N");
        }
    }

    #[test]
    fn smoke_tier_touches_every_stratum() {
        let flops = canonical_flops();
        let order = stratified_order(&flops);
        // Take the first TIER_SMOKE (=100) flops; count distinct strata.
        let smoke: std::collections::BTreeSet<_> = order
            .iter().take(TIER_SMOKE)
            .map(|&i| flop_stratum(flops[i]))
            .collect();
        let all: std::collections::BTreeSet<_> = flops.iter().map(|f| flop_stratum(*f)).collect();
        assert_eq!(smoke.len(), all.len(),
            "first {} flops must cover every stratum (got {} of {})",
            TIER_SMOKE, smoke.len(), all.len());
    }

    #[test]
    fn enumerator_breakdown_matches_theory() {
        let flops = canonical_flops();
        let mut three_distinct = 0;
        let mut paired = 0;
        let mut trips = 0;
        for f in &flops {
            let mut ranks = [f[0] >> 2, f[1] >> 2, f[2] >> 2];
            ranks.sort_unstable();
            if ranks[0] == ranks[1] && ranks[1] == ranks[2] { trips += 1; }
            else if ranks[0] == ranks[1] || ranks[1] == ranks[2] { paired += 1; }
            else { three_distinct += 1; }
        }
        assert_eq!(three_distinct, 1430, "three-distinct-rank shapes wrong");
        assert_eq!(paired,        312,  "pair+kicker shapes wrong");
        assert_eq!(trips,         13,   "trips shapes wrong");
    }
}
