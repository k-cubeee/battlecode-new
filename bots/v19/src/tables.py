"""Static game data: camp stats, champion stats, shop builds, PvP weights.

The shop builds below are only the *defaults*: build_for() prefers whatever the advisor tool
exported into draftdata.py (BUILDS / BUILDS_MAP) and falls back to these when there is no data.
"""

# Camp kinds (cambc.CampType values).
WOLF, ELK, GEM, GOLD, MONO = 0, 1, 2, 3, 4
CAMP_HP = (1350.0, 2400.0, 500.0, 500.0, 20000.0)
CAMP_ST = (20.0, 60.0, 0.0, 0.0, 200.0)


def camp_hp(kind):
    """Health of a camp kind; a kind this table does not know (a newly added camp) is treated like a wolf."""
    return CAMP_HP[kind] if 0 <= kind < len(CAMP_HP) else CAMP_HP[WOLF]


def camp_st(kind):
    """Damage per turn of a camp kind; unknown kinds are treated like a wolf."""
    return CAMP_ST[kind] if 0 <= kind < len(CAMP_ST) else CAMP_ST[WOLF]

# Champion types (cambc.ChampionType values).
RYTH, VESPER, VENDETTA, CASSI, ISSAC, STELLAR, PRISM, MARCELLA, WILLOW, REN, VARG, TAVI, HEART = range(13)

# Draft picks (cambc.ChampionDraft values).
D_RYTH, D_VESPER, D_VENDETTA, D_CASSI_ISSAC, D_STELLAR, D_PRISM, D_MARCELLA, D_WILLOW, D_REN, D_VARG, D_TAVI, D_HEART = range(12)
N_PICKS = 12   # draft choices (Heart of Ruin is the newest)

# Default pick preferences per team size, best first (legacy v18 order). The draft scorer uses them
# as a small prior, so with no match data the draft behaves exactly as before.
PREFS = {
    3: [D_VARG, D_RYTH, D_MARCELLA, D_STELLAR, D_WILLOW, D_TAVI],
    4: [D_VARG, D_RYTH, D_STELLAR, D_MARCELLA, D_TAVI, D_WILLOW],
    5: [D_VARG, D_RYTH, D_STELLAR, D_MARCELLA, D_TAVI, D_WILLOW],
}
# 3-pick maps with distant spawns: the extra body of Cassi & Issac wins the pure farming race.
PREFS_FAR_3 = [D_VARG, D_CASSI_ISSAC, D_RYTH, D_MARCELLA, D_STELLAR, D_WILLOW, D_TAVI]


def prefs_for(size, far=False):
    if size == 3 and far:
        return PREFS_FAR_3
    return PREFS.get(size, PREFS[5])


def bodies_of_pick(pick):
    """Champion types created by one draft pick."""
    if pick == D_CASSI_ISSAC:
        return (CASSI, ISSAC)
    return (pick if pick < D_CASSI_ISSAC else pick + 1,)


# health, strength, focus, health growth, strength growth, focus growth
CHAMP = {
    RYTH: (1200, 50, 100, 100, 5, 10),
    VESPER: (900, 45, 100, 100, 5, 10),
    VENDETTA: (800, 70, 100, 100, 5, 10),
    CASSI: (700, 30, 70, 71.25, 3.5, 15),
    ISSAC: (900, 40, 100, 95, 4.75, 10),
    STELLAR: (750, 35, 100, 75, 3.5, 10),
    PRISM: (700, 35, 80, 75, 3.5, 10),
    MARCELLA: (1200, 45, 50, 100, 5, 10),
    WILLOW: (700, 45, 105, 50, 3, 5),
    REN: (900, 40, 100, 100, 5, 10),
    VARG: (1000, 50, 100, 100, 5, 10),
    TAVI: (750, 35, 100, 75, 5, 10),
    HEART: (1000, 40, 100, 100, 4, 12),
}

# Item families (prefixes of the ItemType names) and stats.
FAMILY = ("MONSTER", "POWER", "DURABILITY", "TREASURE", "JUNGLE", "HOME", "BRUISER")
MON, POW, DUR, TRE, JUN, HOME, BRU = range(7)
STAT = ("STRENGTH", "FOCUS", "HEALTH")
STR, FOC, HP = range(3)

# Shop build orders: each entry is a set of five shards (family, stat) that combines into an emblem.
BUILD_FIGHTER = (
    [(JUN, HP), (MON, STR), (TRE, STR), (DUR, HP), (POW, STR), (HOME, HP)]
    + [(POW, STR), (POW, HP)] * 12
)
BUILD_STELLAR = (
    [(JUN, FOC), (MON, FOC), (TRE, FOC), (DUR, HP), (POW, FOC), (HOME, HP)]
    + [(POW, FOC), (POW, HP)] * 12
)
BUILD_RANGED = (
    [(MON, STR), (JUN, HP), (TRE, STR), (DUR, HP), (POW, STR), (HOME, HP)]
    + [(POW, STR), (POW, HP)] * 12
)
BUILD_SUPPORT = (
    [(MON, STR), (JUN, HP), (TRE, STR), (DUR, HP), (POW, STR), (HOME, HP)]
    + [(POW, STR), (POW, HP)] * 12
)


MELEE_BODIES = (RYTH, VENDETTA, ISSAC, MARCELLA, REN, VARG, HEART)


def default_build(ctype, melee=None):
    """The hand-written shop order for a champion body. `melee` overrides the table for bodies not listed in
    MELEE_BODIES (the bot passes its live attack range)."""
    if ctype == STELLAR:
        return list(BUILD_STELLAR)
    if ctype == WILLOW:
        return list(BUILD_SUPPORT)
    if (ctype in MELEE_BODIES) if melee is None else melee:
        return list(BUILD_FIGHTER)
    return list(BUILD_RANGED)


def _valid_build(b):
    if not b or len(b) > 200:
        return False
    for e in b:
        if len(e) != 2 or not (0 <= e[0] < BRU) or not (0 <= e[1] < 3):
            return False        # BRU (bruiser) needs mixed shards, the shop loop only buys one kind
    return True


def build_for(ctype, sig=None, melee=None):
    """Shop order for a champion body: the advisor's data-driven build for this map size if there is one,
    else its map-independent build, else the hand-written default. Entries are (family, stat) pairs."""
    try:
        import draftdata as D
        b = getattr(D, "BUILDS_MAP", {}).get(sig, {}).get(ctype)
        if not _valid_build(b):
            b = getattr(D, "BUILDS", {}).get(ctype)
        if _valid_build(b):
            return [(int(e[0]), int(e[1])) for e in b]
    except Exception:
        pass
    return default_build(ctype, melee)


# ---- PvP data -------------------------------------------------------------------------------
# (spell damage in multiples of Focus per 8-turn cooldown cycle, has an attack reset)
PVP_KIT = {
    RYTH: (1.0, True), VESPER: (0.0, True), VENDETTA: (0.0, True), CASSI: (2.0, False), ISSAC: (0.0, True),
    STELLAR: (3.4, False), PRISM: (2.5, False), MARCELLA: (2.0, True), WILLOW: (0.0, True), REN: (1.25, False),
    VARG: (0.0, True), TAVI: (2.2, True),
    HEART: (1.75, False),     # landing dash 1.75 x Focus per cycle; its missing-health bonus is added in tactics.unit_dps
}
# how much we want a champion dead first (weight on damage dealt to it)
TARGET_W = {
    RYTH: 1.0, VESPER: 1.1, VENDETTA: 1.1, CASSI: 1.3, ISSAC: 1.1, STELLAR: 1.3, PRISM: 1.25, MARCELLA: 0.9,
    WILLOW: 1.6, REN: 1.0, VARG: 0.95, TAVI: 1.3, HEART: 1.0,
}
