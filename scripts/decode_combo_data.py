#!/usr/bin/env python3
"""Decode a combo-v2 .jsonl.zst file produced by dataset_driver (COMBO_DATA=1).

Usage:
    pip install zstandard
    python scripts/decode_combo_data.py data/solves_combo/3BP/0001_2c2d2h.jsonl.zst
    python scripts/decode_combo_data.py data/solves_combo/3BP/0001_2c2d2h.jsonl.zst --densify --limit 1

What the format looks like
--------------------------
Line 1 (header):
  {"type":"header","schema":"combo-v2","matchup":"3BP","flop_idx":1,
   "flop":["2c","2d","2h"],"starting_pot":2000,"effective_stack":19000,
   "combos_oop":["AcAd","AcAh",...],   # length N_oop
   "combos_ip" :["AcAd","AcAh",...]}   # length N_ip

Lines 2..N (one per decision node):
  {
    ...range-aggregate fields (range_eq, nut, etc.)...,
    "actions": ["fold","call","raise_to_2500"],
    "range_strategy": [0.40, 0.30, 0.30],
    "combo_data": {
      "oop": {"idx":[0,2,5,...], "eq":[0.51,...], "w":[0.012,...], "ev":[1850,...]},
      "ip" : {"idx":[1,4,...],   "eq":[...],      "w":[...],       "ev":[...]},
      "strategy": [
         0,            # pure: action index 0 (fold)
         [0.5, 0.5, 0],# mixed: full distribution
         2,            # pure: action 2 (raise)
         ...
      ]
    }
  }

The strategy array is aligned with the **player-to-act's** sparse idx list
(combo_data["oop"]["idx"] if to_act=="O", else combo_data["ip"]["idx"]).
The header's combos_* arrays map sparse idx → "AcAd"-style combo string.

`--densify` expands each record's sparse per-combo arrays into dense arrays
indexed by the full header combo list (zero-filled for combos not present),
then prints the first record's structure as a sanity check.
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import zstandard as zstd
except ImportError:
    sys.exit("pip install zstandard")


def iter_jsonl_zst(path):
    """Yield decoded JSON dicts, one per line, from a .jsonl.zst file."""
    with open(path, "rb") as fh:
        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(fh) as reader:
            buf = b""
            while True:
                chunk = reader.read(1 << 16)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line:
                        yield json.loads(line)
            if buf:
                yield json.loads(buf)


def densify(record, header):
    """Reconstruct dense per-combo arrays from a sparse record.

    Returns a new dict mirroring the legacy v1 layout:
      {oop_equity[N_oop], oop_weights[N_oop], oop_ev[N_oop],
       ip_equity [N_ip],  ip_weights [N_ip],  ip_ev [N_ip],
       strategy[[p0, p1, ...] for each ACTOR combo (zero rows for absent combos)]}
    """
    cd = record.get("combo_data")
    if cd is None:
        return None

    n_oop = len(header["combos_oop"])
    n_ip  = len(header["combos_ip"])
    n_act = len(record["actions"])

    def expand(side, n):
        eq = [0.0] * n
        w  = [0.0] * n
        ev = [0]   * n
        for i, idx in enumerate(side["idx"]):
            eq[idx] = side["eq"][i]
            w [idx] = side["w"] [i]
            ev[idx] = side["ev"][i]
        return eq, w, ev

    oop_eq, oop_w, oop_ev = expand(cd["oop"], n_oop)
    ip_eq,  ip_w,  ip_ev  = expand(cd["ip"],  n_ip)

    actor_n   = n_oop if record["to_act"] == "O" else n_ip
    actor_idx = cd["oop"]["idx"] if record["to_act"] == "O" else cd["ip"]["idx"]
    strategy = [[0.0] * n_act for _ in range(actor_n)]
    for i, entry in enumerate(cd["strategy"]):
        idx = actor_idx[i]
        if isinstance(entry, int):
            strategy[idx][entry] = 1.0
        else:
            strategy[idx] = list(entry)

    return {
        "oop_equity": oop_eq, "oop_weights": oop_w, "oop_ev": oop_ev,
        "ip_equity":  ip_eq,  "ip_weights":  ip_w,  "ip_ev":  ip_ev,
        "strategy":   strategy,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help=".jsonl.zst file from dataset_driver (COMBO_DATA=1)")
    ap.add_argument("--densify", action="store_true",
                    help="Densify the first --limit records and print their shapes")
    ap.add_argument("--limit", type=int, default=1,
                    help="Records to densify when --densify is given (default 1)")
    args = ap.parse_args()

    it = iter_jsonl_zst(args.path)
    header = next(it)
    if header.get("type") != "header":
        sys.exit(f"first line is not a header: {header!r}")
    if header.get("schema") != "combo-v2":
        sys.exit(f"unsupported schema: {header.get('schema')!r}")

    print(f"File:           {args.path}")
    print(f"Matchup:        {header['matchup']}  flop_idx={header['flop_idx']}  "
          f"flop={'-'.join(header['flop'])}")
    print(f"Combos:         OOP={len(header['combos_oop'])}  IP={len(header['combos_ip'])}")
    print(f"Stacks:         start_pot={header['starting_pot']}  eff_stack={header['effective_stack']}")
    print()

    densified = 0
    n = 0
    for rec in it:
        n += 1
        if args.densify and densified < args.limit:
            d = densify(rec, header)
            if d is not None:
                print(f"--- record {n} ---")
                print(f"  history:        {rec['history']}")
                print(f"  board:          {rec['board']}")
                print(f"  to_act:         {rec['to_act']}")
                print(f"  actions:        {rec['actions']}")
                print(f"  range_strategy: {rec['range_strategy']}")
                cd = rec["combo_data"]
                print(f"  sparse: oop nnz={len(cd['oop']['idx'])}/{len(header['combos_oop'])}  "
                      f"ip nnz={len(cd['ip']['idx'])}/{len(header['combos_ip'])}")
                pure = sum(1 for e in cd["strategy"] if isinstance(e, int))
                print(f"  strategy: rows={len(cd['strategy'])}  pure={pure}  "
                      f"mixed={len(cd['strategy']) - pure}")
                # Sanity: a couple of dense values
                actor_idx = cd['oop']['idx'] if rec['to_act'] == 'O' else cd['ip']['idx']
                if actor_idx:
                    first = actor_idx[0]
                    cstr = (header['combos_oop'] if rec['to_act'] == 'O'
                            else header['combos_ip'])[first]
                    print(f"  e.g. first actor combo: idx={first}  combo={cstr}  "
                          f"strategy={d['strategy'][first]}")
                densified += 1
    print(f"\nTotal node records: {n}")


if __name__ == "__main__":
    main()
