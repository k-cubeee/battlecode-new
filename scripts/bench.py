"""Benchmark one bot against another across maps, playing both sides of every map.

Usage (from the workspace root):
    python3 scripts/bench.py my-bot starter-python              # every map in maps/
    python3 scripts/bench.py my-bot v19 --maps 00ambush 01islands
    python3 scripts/bench.py my-bot v19 --sample 10 --jobs 4     # 10 maps spread across the pool

Each map is played twice, once with your bot as team A and once as team B (team B wins exact ties,
so sides matter). Replays and per-match statistics go to replays/bench/<bots>-<time>/; open any
replay with `cambc view <path>`.
"""
import argparse
import collections
import glob
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pick_maps(args):
    every = sorted(os.path.basename(p)[:-len(".map26a")] for p in glob.glob(os.path.join(ROOT, "maps", "*.map26a")))
    if args.maps:
        missing = [m for m in args.maps if m not in every]
        if missing:
            sys.exit(f"unknown maps: {missing}")
        return args.maps
    if args.sample and args.sample < len(every):
        step = len(every) / args.sample
        return [every[int(i * step)] for i in range(args.sample)]
    return every


def write_manifest(out, ours, theirs, maps):
    bot = lambda name: os.path.join(ROOT, "bots", name)
    lines = []
    for m in maps:
        path = os.path.join(ROOT, "maps", m + ".map26a")
        for side, a, b in (("A", ours, theirs), ("B", theirs, ours)):
            stem = os.path.join(out, f"{side}_{m}")
            lines.append(f'"{bot(a)}" "{bot(b)}" "{path}" "{stem}.repl26a" --statistics "{stem}.json" --json')
    manifest = os.path.join(out, "matches.txt")
    with open(manifest, "w") as f:
        f.write("\n".join(lines) + "\n")
    return manifest


def read_match(path):
    """One row per match, seen from our bot's side (team A in A_*.json, team B in B_*.json)."""
    side, _, mapname = os.path.basename(path)[:-len(".json")].partition("_")
    us, them = ("teamA", "teamB") if side == "A" else ("teamB", "teamA")
    with open(path) as f:
        d = json.load(f)
    gems = {t["team"]: int(t["submittedGems"]) for t in d["teams"]}
    deaths = collections.Counter()
    fuel_outs = 0
    for e in d["events"]:
        team = e["owner"].get("team")
        if e["kind"] == "death":
            for x in e["entries"]:
                deaths[(team, "camp" if x.get("context") == "camp" else "champion")] += 1
        elif e["kind"] == "fuelExhausted" and team == us:
            fuel_outs += len(e["entries"])
    return {
        "map": mapname, "side": side, "win": d["match"]["outcome"] == us, "rounds": d["match"]["roundsCompleted"],
        "ours": gems[us], "theirs": gems[them],
        "camp_deaths": deaths[(us, "camp")], "pvp_deaths": deaths[(us, "champion")],
        "their_camp_deaths": deaths[(them, "camp")], "their_pvp_deaths": deaths[(them, "champion")],
        "fuel_outs": fuel_outs,
        "draft": [c["name"] for c in sorted(d["roster"]["champions"], key=lambda c: c["draftPick"]) if c["team"] == us],
    }


def summarise(rows, ours, theirs):
    n = len(rows)
    wins = sum(r["win"] for r in rows)
    print(f"\n{ours} vs {theirs}: won {wins}/{n} ({100 * wins / n:.0f}%)", end="")
    for side in "AB":
        s = [r for r in rows if r["side"] == side]
        if s:
            print(f"  | as team {side}: {sum(r['win'] for r in s)}/{len(s)}", end="")
    print()
    mean = lambda k: sum(r[k] for r in rows) / n
    print(f"  gems tributed      {mean('ours'):7.1f} vs {mean('theirs'):.1f}")
    print(f"  deaths to camps    {mean('camp_deaths'):7.1f} vs {mean('their_camp_deaths'):.1f}")
    print(f"  deaths to enemies  {mean('pvp_deaths'):7.1f} vs {mean('their_pvp_deaths'):.1f}")
    print(f"  game length        {mean('rounds'):7.0f} rounds")
    print(f"  fuel run-outs      {mean('fuel_outs'):7.1f} per game (turns where {ours} ran out of compute)")
    print("\n  map                       side  result     gems  camp-d  pvp-d  draft")
    for r in sorted(rows, key=lambda r: (r["win"], r["ours"] - r["theirs"])):
        result = "WIN " if r["win"] else "LOSS"
        print(f"  {r['map']:25s} {r['side']:>4s}  {result} {r['ours']:5d}-{r['theirs']:<5d} {r['camp_deaths']:6d} {r['pvp_deaths']:6d}  {', '.join(r['draft'])}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ours", help="your bot (a folder name in bots/)")
    p.add_argument("theirs", help="the opponent bot (a folder name in bots/)")
    p.add_argument("--maps", nargs="+", help="map names (default: every map in maps/)")
    p.add_argument("--sample", type=int, help="use this many maps spread evenly across the pool")
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="matches to run at once")
    p.add_argument("--rounds", type=int, help="stop each match early (results are then incomplete)")
    args = p.parse_args()
    for name in (args.ours, args.theirs):
        if not os.path.isdir(os.path.join(ROOT, "bots", name)):
            sys.exit(f"no bot named {name!r} in bots/")

    maps = pick_maps(args)
    out = os.path.join(ROOT, "replays", "bench", f"{args.ours}-vs-{args.theirs}-{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out)
    manifest = write_manifest(out, args.ours, args.theirs, maps)
    cmd = ["cambc", "run-batch", manifest, "--jobs", str(args.jobs)]
    if args.rounds:
        cmd += ["--rounds", str(args.rounds)]
    print(f"Running {2 * len(maps)} matches ({len(maps)} maps x 2 sides) with {args.jobs} at a time...")
    print(f"Output: {os.path.relpath(out, ROOT)}")
    with open(os.path.join(out, "batch.log"), "w") as log:
        result = subprocess.run(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    rows = [read_match(path) for path in sorted(glob.glob(os.path.join(out, "[AB]_*.json")))]
    if not rows:
        sys.exit(f"no matches finished; see {os.path.join(out, 'batch.log')}")
    if result.returncode != 0 or len(rows) < 2 * len(maps):
        print(f"warning: only {len(rows)} of {2 * len(maps)} matches finished; see batch.log")
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(rows, f, indent=1)
    summarise(rows, args.ours, args.theirs)


if __name__ == "__main__":
    main()
