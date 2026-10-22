"""Draft scoring from match history. Pure Python (no cambc), used by draft.py and by advisor.py.

Every candidate pick gets a score in log-odds of winning:

    score = base + prior + map + vs + syn + slot

  base   how often teams containing the champion win (all maps, all opponents)
  prior  small bonus for the hand-picked order in tables.PREFS (fades in importance as data arrives)
  map    how much better/worse the champion does on this map size than its base rate
  vs     how much better/worse it does against the enemy champions already picked, beyond what
         those champions' own strength explains (summed, divided by sqrt(number of enemy picks)
         so a long list does not double count)
  syn    the same for our own teammates already picked, beyond the teammates' own strength
  slot   the same for being picked in this position of the draft (1st, 2nd, ...)

Each effect is a shrunk estimate: (wins + K * base_rate) / (games + K). With few games it stays at
the base rate, so thin data cannot swing a pick. The same formulas live in board_template.html.

`counts` is a dict:
    {"base": {c: (games, wins)}, "map": {sig: {c: (games, wins)}},
     "vs": {c: {enemy: (games, wins)}}, "syn": {c: {mate: (games, wins)}}, "slot": {c: {index: (games, wins)}}}
"""
import math

# Shrinkage strengths, in "virtual games at the prior". A win/loss result carries about 4 units of variance
# in log-odds, so an effect of typical size tau needs roughly 4 / tau^2 games before it outweighs the noise:
# base tau~0.25 -> ~60, map/vs/syn tau~0.2 -> ~80 (map 60), slot tau~0.16 -> ~120. Lower them if you have
# very few matches and want the advisor to react faster (at the cost of chasing noise).
K_BASE = 60.0
K_MAP = 60.0
K_VS = 80.0
K_SYN = 80.0
K_SLOT = 120.0
PRIOR_STEP = 0.05   # log-odds per rank step of the hand-picked order

# Shop-build planning (advisor.py; same numbers in board_template.html). An emblem effect is a few percent of
# win probability, hence the large K: it takes several hundred owners before a lift is believed.
K_EMB = 250.0
K_EMB_MAP = 250.0
K_STRATUM = 5.0
DEFAULT_BONUS = 0.15    # log-odds credit for the hand-written build's choices
DROP_BELOW = -0.10      # a passive emblem scoring worse than this is replaced by Power shards
TAIL_SETS = 24          # Power sets after the passives
TAIL_OFF_DEFAULT = 0.15  # weight of a Power stat the hand-written build does not use (before evidence)
TAIL_SLOPE = 6.0        # how strongly a Power stat's lift shifts its share of the tail
MIN_BUILD_GAMES = 10    # games of a champion body before its build is data-driven
MIN_MAP_GAMES = 6       # ... and before a map size gets its own build

_PARAM_NAMES = ("K_BASE", "K_MAP", "K_VS", "K_SYN", "K_SLOT", "PRIOR_STEP", "K_EMB", "K_EMB_MAP", "K_STRATUM", "DEFAULT_BONUS",
                "DROP_BELOW", "TAIL_SETS", "TAIL_OFF_DEFAULT", "TAIL_SLOPE", "MIN_BUILD_GAMES", "MIN_MAP_GAMES")


def params():
    """Every tunable above, for the HTML board (so JS and Python cannot drift apart)."""
    g = globals()
    return {k: g[k] for k in _PARAM_NAMES}


def logit(p):
    return math.log(p / (1.0 - p))


def base_p(counts, c):
    n, w = counts["base"].get(c, (0, 0))
    return (w + K_BASE * 0.5) / (n + K_BASE)


def _delta(nw, p_c, k):
    if not nw:
        return 0.0
    n, w = nw
    return logit((w + k * p_c) / (n + k)) - logit(p_c)


def prior_bonus(prefs):
    """Bonus per champion from an ordered preference list (best first)."""
    n = len(prefs)
    return {c: PRIOR_STEP * (n - i) for i, c in enumerate(prefs)}


def terms(counts, c, sig, ours, theirs, prior=0.0):
    """Score of picking c next, broken into its parts (all in log-odds)."""
    p_c = base_p(counts, c)
    t = {"base": logit(p_c), "prior": prior}
    t["map"] = _delta(counts["map"].get(sig, {}).get(c), p_c, K_MAP)
    # Against e our win rate falls when e is strong, with m it rises when m is strong: remove that part
    # (it is the same for every candidate) and keep the interaction.
    vs = counts["vs"].get(c, {})
    t["vs"] = sum(_delta(vs.get(e), p_c, K_VS) + logit(base_p(counts, e)) for e in theirs) / math.sqrt(len(theirs)) if theirs else 0.0
    syn = counts["syn"].get(c, {})
    t["syn"] = sum(_delta(syn.get(m), p_c, K_SYN) - logit(base_p(counts, m)) for m in ours) / math.sqrt(len(ours)) if ours else 0.0
    t["slot"] = _delta(counts["slot"].get(c, {}).get(len(ours)), p_c, K_SLOT)
    t["total"] = t["base"] + t["prior"] + t["map"] + t["vs"] + t["syn"] + t["slot"]
    return t


def recommend(counts, sig, ours, theirs, candidates, prior=None):
    """Candidates ranked best first: [(champion, terms)]. Ties keep the candidate order."""
    prior = prior or {}
    ours = list(ours)
    theirs = list(theirs)
    rows = [(c, terms(counts, c, sig, ours, theirs, prior.get(c, 0.0))) for c in candidates if c not in ours]
    rows.sort(key=lambda r: -r[1]["total"])
    return rows


def best_pick(counts, sig, ours, theirs, candidates, prior=None):
    rows = recommend(counts, sig, ours, theirs, candidates, prior)
    return rows[0][0] if rows else None


def complete(counts, sig, ours, theirs, size, candidates, prior=None):
    """Greedy projection of our whole team: keep taking the best pick until `size` champions are chosen."""
    team = list(ours)
    while len(team) < size:
        c = best_pick(counts, sig, team, theirs, candidates, prior)
        if c is None:
            break
        team.append(c)
    return team


def win_prob(total):
    return 1.0 / (1.0 + math.exp(-total))
