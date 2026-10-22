"""Pure tactical maths for the bot (no cambc import, so it can be unit-tested on its own).

  unit_dps        average damage per turn of any champion type (unknown types fall back to plain strength)
  hit_turn        on which of our turns an enemy can first hit a tile
  incoming_over   damage we take over the next N turns if we stand still
  fight_sim       turn-by-turn duel/brawl simulation: do we win, and with how much health left
  target_weight   how much we want one enemy hit, compared with another

Everything works for every champion, including ones this file has never heard of: the kit tables are only
consulted with .get(), and a body that is not listed is judged by its live strength, focus and range.
"""
import tables as T

# feature switches (used to measure each rule in simulation; leave True unless a test shows a rule hurts)
USE_SIM_GATE = False       # fights need a winning brawl simulation, not just the strength ratio
USE_CAPTURE_HOLD = False   # no recall while a capture is about to pay out
USE_RECALL_SAFE = True    # recall only when nothing can hit us first
USE_CORNER = False         # cornered by a melee champion: fight back
USE_CAMP_THREAT = True    # awake camps in reach count against starting a recall

USE_MONO_VETO = True    # never take a group that contains a monolith
DEATH_BLOCK = 0           # rounds a group stays off our list after it killed us (times the number of deaths there); 0 = off
BANK_A, BANK_B, BANK_MIN, BANK_MAX = 6, 10, 30, 110   # gems carried before a bank trip = A * (walk to altar) + B, clamped
CAMP_REACH = 4            # tiles a camp can hit from (the rules say 3; replays suggest it can be 4)
PARTY_FRAC = 0.0          # a camp group hitting harder than this share of our health per turn is only started in company (0 = off)
PARTY_MIN = 1             # teammates that must be within escort range before we start such a group
CAMP_MARGIN = 1.0         # multiplies how much health spare we want before leaving a camp fight

SPAWN_HEAL_W = 3.0     # an enemy standing on its own spawn heals every turn: count its health this many times
FOE_BURST = 1.15       # we cannot see enemy cooldowns: assume a little extra damage in the first turns
FOE_BURST_TURNS = 2
HORIZON = 16


def unit_dps(ctype, s, f):
    """Average damage per turn of a champion over a full cooldown cycle."""
    mods, reset = T.PVP_KIT.get(ctype, (0.0, False))
    d = s + mods * f / 9.0
    if reset:
        d += s / 9.0
    if ctype == T.HEART:
        d *= 1.2          # passive: up to +50% damage as health drops, ~+20% averaged over a fight
    return d


def is_melee_type(ctype):
    return ctype in T.MELEE_BODIES


def reach_of(attack_range):
    """Tiles an enemy can hit from: ranged champions use their range, melee ones threaten the ring they can step into."""
    return 2 if attack_range <= 1 else attack_range


def hit_turn(dist, attack_range):
    """The first of its turns (1, 2, ...) on which an enemy `dist` tiles away can hit us if we do not move.
    A melee champion steps and strikes in one turn; a ranged one must either move or shoot."""
    if dist <= attack_range:
        return 1
    steps = dist - attack_range
    return steps if attack_range <= 1 else steps + 1


def incoming_over(px, py, enemies, turns, cheb, skip=None, camp_dps=0.0):
    """(damage, first_hit_turn) we take over `turns` turns standing at (px, py).
    enemies: the bot's enemy tuples. skip(e) -> True for enemies that will not come (loitering on their spawn)."""
    total = camp_dps * turns
    first = 99
    for e in enemies:
        d = cheb(px, py, e[0], e[1])
        if skip is not None and skip(e) and d > e[6] + 2:
            continue
        t0 = hit_turn(d, e[6])
        if t0 > turns:
            continue
        if t0 < first:
            first = t0
        total += unit_dps(e[8], e[3], e[7]) * (turns - t0 + 1)
    if camp_dps > 0.0 and first > 1:
        first = 1
    return total, first


def target_weight(ctype, hp, maxhp, dps, mean_dps, gems):
    """Weight on damage dealt to one enemy: champions we want dead first (healers, glass cannons), the ones already hurt,
    the ones that deal the most damage for the health they have, and the ones carrying a bag (the killer takes it)."""
    frac = hp / maxhp if maxhp > 0 else 1.0
    w = T.TARGET_W.get(ctype, 1.0) * (1.0 + (1.0 - frac))
    if mean_dps > 0.0:
        k = 0.7 + 0.6 * dps / mean_dps
        w *= 0.7 if k < 0.7 else (1.5 if k > 1.5 else k)
    w *= 1.0 + 0.8 * (gems if gems < 150 else 150) / 150.0
    return w


def fight_sim(mine, foes, burst_me=0.0, env_dps=0.0, horizon=HORIZON):
    """Simulate a brawl. mine: [(hp, dps), ...] with the bot first; foes: [(hp, dps), ...].
    Both sides hit every turn; we split our damage onto the foe that dies fastest, they go for our weakest.
    Returns (win, my_hp_left, turns): win means every foe died while the bot was still standing."""
    me = [[float(h), float(d)] for h, d in mine]
    fo = [[float(h), float(d)] for h, d in foes]
    if not fo:
        return True, me[0][0] if me else 0.0, 0
    if not me:
        return False, 0.0, 0
    order = sorted(range(len(fo)), key=lambda i: fo[i][0] / max(fo[i][1], 1.0))
    for t in range(horizon):
        my_dmg = sum(m[1] for m in me if m[0] > 0.0) + (burst_me if t == 0 else 0.0)
        foe_dmg = sum(f[1] for f in fo if f[0] > 0.0)
        if t < FOE_BURST_TURNS:
            foe_dmg *= FOE_BURST
        foe_dmg += env_dps
        d = my_dmg
        for i in order:
            if fo[i][0] > 0.0 and d > 0.0:
                take = fo[i][0] if fo[i][0] < d else d
                fo[i][0] -= take
                d -= take
        d = foe_dmg
        for m in sorted((m for m in me if m[0] > 0.0), key=lambda m: m[0]):
            if d <= 0.0:
                break
            take = m[0] if m[0] < d else d
            m[0] -= take
            d -= take
        foes_dead = all(f[0] <= 0.0 for f in fo)
        me_dead = me[0][0] <= 0.0
        if foes_dead and not me_dead:
            return True, me[0][0], t + 1
        if me_dead:
            return False, 0.0, t + 1
    return False, me[0][0], horizon
