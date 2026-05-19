//! Loader for HU 200 BB preflop ranges from a plain-text template file.
//!
//! Source of truth: `data/hu_200bb_ranges.txt` at the repo root. That file
//! holds raw user-pasted range strings. This module parses it into typed
//! `Range`s at runtime.
//!
//! ## Why runtime-loaded instead of `const`?
//!
//! Range exports are large, change shape between scenarios, and are easy to
//! get wrong. Editing a plain-text template is friction-free; editing huge
//! `const &str` literals in a `.rs` file is not. The parse happens once at
//! startup; the typed structure is held in memory for the rest of the run.
//!
//! ## Typical use
//!
//! ```ignore
//! use postflop_solver::hu_200bb_ranges::{PreflopRanges, Action};
//!
//! let ranges = PreflopRanges::load_default().unwrap();
//! let sb_raise = ranges.get("SB_FIRST_ACTION", Action::Raise)
//!     .expect("SB raise range not provided");
//! ```

use crate::Range;
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

/// Action labels at each preflop decision node.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Action {
    /// All combos dealt to the player (typically weight 1 per combo).
    All,
    /// The aggressive action: raise/3-bet/4-bet/iso-raise.
    Raise,
    /// The passive continuation: call/limp/check.
    Call,
    /// The terminal action: fold.
    Fold,
}

impl Action {
    fn from_marker(s: &str) -> Option<Self> {
        match s.trim_end_matches(':').trim().to_ascii_uppercase().as_str() {
            "ALL"   => Some(Self::All),
            "RAISE" => Some(Self::Raise),
            "CALL"  => Some(Self::Call),
            "FOLD"  => Some(Self::Fold),
            _ => None,
        }
    }
}

/// Per-spot action breakdown.
#[derive(Debug, Default)]
pub struct Spot {
    pub all:   Option<Range>,
    pub raise: Option<Range>,
    pub call:  Option<Range>,
    pub fold:  Option<Range>,
}

impl Spot {
    pub fn get(&self, action: Action) -> Option<&Range> {
        match action {
            Action::All   => self.all.as_ref(),
            Action::Raise => self.raise.as_ref(),
            Action::Call  => self.call.as_ref(),
            Action::Fold  => self.fold.as_ref(),
        }
    }
    fn set(&mut self, action: Action, range: Range) {
        match action {
            Action::All   => self.all   = Some(range),
            Action::Raise => self.raise = Some(range),
            Action::Call  => self.call  = Some(range),
            Action::Fold  => self.fold  = Some(range),
        }
    }
}

/// All preflop ranges, keyed by spot name (e.g. `"SB_FIRST_ACTION"`).
#[derive(Debug, Default)]
pub struct PreflopRanges {
    pub spots: BTreeMap<String, Spot>,
}

impl PreflopRanges {
    /// Default template path relative to the repo root.
    pub const DEFAULT_PATH: &'static str = "data/hu_200bb_ranges.txt";

    /// Look up a spot.
    pub fn spot(&self, name: &str) -> Option<&Spot> {
        self.spots.get(name)
    }

    /// Convenience: get one range directly.
    pub fn get(&self, spot: &str, action: Action) -> Option<&Range> {
        self.spots.get(spot)?.get(action)
    }

    /// Parse from the template text.
    pub fn from_str(text: &str) -> Result<Self, String> {
        let mut ranges = PreflopRanges::default();
        let mut current_spot: Option<String> = None;
        let mut current_action: Option<Action> = None;
        let mut buf = String::new();

        let mut commit = |ranges: &mut PreflopRanges,
                          spot: &Option<String>,
                          action: Option<Action>,
                          buf: &mut String| -> Result<(), String> {
            let content = buf.trim();
            if !content.is_empty() {
                let spot_name = spot.as_ref().ok_or_else(||
                    "range content appeared outside any === SPOT === section".to_string())?;
                let action = action.ok_or_else(||
                    format!("range content in [{spot_name}] outside any ALL/RAISE/CALL/FOLD marker"))?;
                let label = format!("{spot_name}/{action:?}");
                let r = parse_range_str(&label, content)?;
                let entry = ranges.spots.entry(spot_name.clone()).or_default();
                entry.set(action, r);
            }
            buf.clear();
            Ok(())
        };

        for raw in text.lines() {
            let line = raw.trim();
            if line.is_empty() || line.starts_with('#') { continue; }

            if let Some(name) = parse_section_header(line) {
                commit(&mut ranges, &current_spot, current_action, &mut buf)?;
                current_spot = Some(name);
                current_action = None;
                continue;
            }

            if let Some(action) = Action::from_marker(line) {
                // Action markers look like `ALL:` or `RAISE: <content>`.
                // Anything after the colon on the same line is content.
                commit(&mut ranges, &current_spot, current_action, &mut buf)?;
                current_action = Some(action);

                if let Some(idx) = line.find(':') {
                    let rest = line[idx+1..].trim();
                    if !rest.is_empty() {
                        buf.push_str(rest);
                    }
                }
                continue;
            }

            // Regular content line — append.
            if !buf.is_empty() { buf.push(' '); }
            buf.push_str(line);
        }
        commit(&mut ranges, &current_spot, current_action, &mut buf)?;
        Ok(ranges)
    }

    /// Load from a file path.
    pub fn load_file(path: impl AsRef<Path>) -> Result<Self, String> {
        let p = path.as_ref();
        let text = fs::read_to_string(p)
            .map_err(|e| format!("failed to read {}: {e}", p.display()))?;
        Self::from_str(&text)
    }

    /// Load from the default path relative to `CARGO_MANIFEST_DIR` if
    /// available (works in tests + `cargo run`), falling back to CWD.
    pub fn load_default() -> Result<Self, String> {
        let candidate: PathBuf = match std::env::var("CARGO_MANIFEST_DIR") {
            Ok(dir) => Path::new(&dir).join(Self::DEFAULT_PATH),
            Err(_)  => PathBuf::from(Self::DEFAULT_PATH),
        };
        Self::load_file(candidate)
    }
}

/// Parse a single range string, sanitizing exporter floating-point noise.
///
/// Two classes of noise get cleaned up:
/// - Weights slightly above 1 (e.g. `1.0001`): the postflop-solver parser
///   rejects weights > 1, so clamp them to `1`.
/// - Weights in scientific notation (e.g. `5.9e-7`): the parser's regex
///   only accepts decimal literals. These are always near-zero export
///   artifacts, so substitute `0` (the entry is effectively skipped).
pub fn parse_range_str(name: &str, s: &str) -> Result<Range, String> {
    if s.is_empty() {
        return Err(format!("range `{name}` is empty"));
    }
    // Step 1: clamp slight over-1 noise (textual substitution is safe because
    // these specific tokens never appear as substrings of other valid numbers).
    let mut cleaned = s.replace("1.0001", "1").replace("1.0002", "1");
    // Step 2: replace any scientific-notation weight (e.g. `5.9e-7`, `2e-8`)
    // with `0`. Pattern: digit(s) [optional `.digits`] `e` `-` digit(s).
    let sci = regex::Regex::new(r"\d+(?:\.\d+)?[eE]-\d+").unwrap();
    cleaned = sci.replace_all(&cleaned, "0").into_owned();
    cleaned.parse::<Range>()
        .map_err(|e| format!("range `{name}` failed to parse: {e}"))
}

fn parse_section_header(line: &str) -> Option<String> {
    let s = line.trim();
    let inner = s.strip_prefix("===").and_then(|s| s.strip_suffix("==="))?;
    Some(inner.trim().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_minimal() {
        let txt = "=== TEST ===\nALL:\n2c2d: 1, 3c3d: 1\nRAISE:\n2c2d: 0.5\n";
        let r = PreflopRanges::from_str(txt).unwrap();
        assert!(r.get("TEST", Action::All).is_some());
        assert!(r.get("TEST", Action::Raise).is_some());
        assert!(r.get("TEST", Action::Call).is_none());
    }

    #[test]
    fn loads_default_template() {
        let r = PreflopRanges::load_default().expect("template must be parseable");
        // SB_FIRST_ACTION should at least be present (even if its individual
        // actions are empty placeholders).
        assert!(r.spot("SB_FIRST_ACTION").is_some(), "SB_FIRST_ACTION section missing");
    }

    /// At every spot, the sum of per-combo action weights (raise + call + fold)
    /// must be ≤ 1.0 per combo. This holds regardless of whether the exporter
    /// uses a "conditional on reaching this node" or "joint with reach
    /// probability" convention — both produce per-combo weights bounded by 1.
    ///
    /// Catches: export bugs, double-counting, weights > 1, and combos that
    /// have weight in multiple mutually-exclusive actions.
    #[test]
    fn per_combo_action_sums_le_one_at_every_spot() {
        let r = PreflopRanges::load_default().expect("template must be parseable");
        let mut report: Vec<(String, f32)> = Vec::new();
        for (name, spot) in &r.spots {
            let raise = spot.raise.as_ref().map(|r| r.raw_data());
            let call  = spot.call .as_ref().map(|r| r.raw_data());
            let fold  = spot.fold .as_ref().map(|r| r.raw_data());

            let len = raise.map(|d| d.len())
                .or_else(|| call.map(|d| d.len()))
                .or_else(|| fold.map(|d| d.len()));
            let Some(n) = len else { continue; };

            let mut max_sum: f32 = 0.0;
            for i in 0..n {
                let r = raise.map_or(0.0, |d| d[i]);
                let c = call .map_or(0.0, |d| d[i]);
                let f = fold .map_or(0.0, |d| d[i]);
                let s = r + c + f;
                if s > max_sum { max_sum = s; }
                assert!(
                    s <= 1.001,
                    "spot `{name}` combo idx {i}: raise+call+fold = {s:.4} > 1.0"
                );
            }
            report.push((name.clone(), max_sum));
        }
        for (name, max_sum) in &report {
            eprintln!("  {name}: max (raise+call+fold) per combo = {max_sum:.4}");
        }
    }
}
