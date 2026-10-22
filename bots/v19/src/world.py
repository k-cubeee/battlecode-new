"""World model: camp groups, distance fields, shared claims and work selection.

Adapted from v18's proven farming core: every group is scored by
(gems + decaying gold value) per turn of work, simulated before committing, and
claimed in shared memory (one byte per slot: rank+1 in the low six bits, phase in
the top two) so teammates split the map instead of fighting over one camp.
"""
import cambc
from cambc import game_map
import tables as T
import tactics as X

ALLY = cambc.Team.ALLY
ENEMY = cambc.Team.ENEMY
Vec2 = cambc.Vec2
Bitboard = cambc.Bitboard
DistanceField = cambc.DistanceField
Error = cambc.Error

DEBUG = False           # one status line per turn (costs fuel; leave off)
UNREACHED = 65535
CAP_TURNS = 5           # rounds of standing on the spawn tiles to capture a cleared group
CAPTURE_TIMEOUT = 25    # rounds we are willing to spend capturing before giving up
CAP_PATIENCE = 4        # turns waiting for a poorer ally to take a spawn tile
WAIT_NEAR_MAX = 16      # longest respawn wait we will spend standing next to a group
K_PARTY = 2             # most champions that may work on the same camp group at once
BIG_GEMS = 150          # packs worth this many gems are worth a bigger party ...
BIG_PARTY = 3           # ... of up to this many champions
BIG_EFF = 1.25          # joining a big pack may cost at most this much more champion-time
NEAR_K = 30             # groups (nearest our spawn first) examined when choosing work
CHOOSE_MIN_FUEL = 1500000   # choosing a task can cost ~1M fuel; defer it when less is left
KAPPA0 = 0.3            # value of one gold in gems at round 0 ...
KAPPA_MIN = 0.05        # ... falling linearly to this floor ...
KAPPA_DECAY = 1200.0    # ... over this many rounds
ROUND_LIMIT = 2000      # carried gems do not count when the game ends here
ENDGAME_SLACK = 8       # extra turns allowed for the last walk to an altar
BANK_LATCH = 30         # once we set out to bank we keep at it this long
DENY_W = 3.0            # extra weight on groups on the enemy's half when we are contesting
SIEGE_D = 30.0          # groups within this distance of the enemy spawn get the bonus
ESP_AVOID = 6           # while contesting, skip groups this close to the enemy spawn (they heal there)
ESCORT_R = 6            # allies this close count as company when choosing where to go
HOME_OPEN_SLOTS = 4     # with this many champions v18 plays a stay-home opening ...
OPEN_HOME_R = 300       # ... until this round: nobody contests deep, so we do not either

# ability kinds
K_NONE, K_RESET, K_DAMAGE, K_AOE, K_CC, K_HEAL, K_DASH, K_SHIELD = range(8)
_ABILITY_CLASSES = (
    (cambc.AbilityAttackReset, K_RESET), (cambc.AbilityDamage, K_DAMAGE),
    (cambc.AbilityAoeDamage, K_AOE), (cambc.AbilityCc, K_CC), (cambc.AbilityHeal, K_HEAL),
    (cambc.AbilityDash, K_DASH), (cambc.AbilityShield, K_SHIELD),
)

# modes
M_IDLE, M_HOME, M_BANK = 0, 1, 2
# claim phases stored in the top two bits of a shared-memory byte
C_HEAD, C_STAGE, C_FIGHT, C_DONE = 0, 1, 2, 3


class Grp:
    """Static facts about one camp group plus what we remember about it."""
    __slots__ = ("i", "cg", "xy", "vec", "bb", "gems", "gold", "resp", "kind", "hp", "st", "n", "adj",
                 "cx", "cy", "rank", "d_spawn", "far", "ready_at", "block_until", "hpleft", "hp_ver",
                 "fails", "est_key", "est", "xyset", "v0", "stsum", "hp_total", "hpsum", "contest", "d_enemy", "deny")


def is_big(g):
    """A rich pack of several monsters; a lone monolith is one long static fight."""
    return g.gems >= BIG_GEMS and g.n >= 2


def cheb(ax, ay, bx, by):
    dx = ax - bx
    dy = ay - by
    if dx < 0:
        dx = -dx
    if dy < 0:
        dy = -dy
    return dx if dx > dy else dy


def simulate(hps, sts, adj, dps, splash, mult):
    """Time to clear a group and damage taken, killing the highest threat-per-hp camp first."""
    n = len(hps)
    hp = list(hps)
    alive = [True] * n
    t = 0.0
    taken = 0.0
    for _ in range(n + 1):
        prim = -1
        best = -1.0
        for i in range(n):
            if alive[i]:
                key = sts[i] / hp[i] * 1000.0 - hp[i] * 1e-6
                if key > best:
                    best = key
                    prim = i
        if prim < 0:
            break
        rates = [0.0] * n
        rates[prim] = dps
        if splash > 0.0:
            row = adj[prim]
            for j in range(n):
                if alive[j] and row[j]:
                    rates[j] = splash
        dt = 1e18
        inc = 0.0
        for j in range(n):
            if alive[j]:
                inc += sts[j]
                if rates[j] > 0.0:
                    d = hp[j] / rates[j]
                    if d < dt:
                        dt = d
        taken += inc * dt * mult
        t += dt
        for j in range(n):
            if alive[j] and rates[j] > 0.0:
                hp[j] -= rates[j] * dt
                if hp[j] <= 1e-6:
                    alive[j] = False
    return t, taken


class WorldMixin:
    """Map state, team communication and the choice of which camp group to work next."""

    # ------------------------------------------------------------------ logging
    def log(self, msg):
        if DEBUG:
            print(f"R{self.rnd} S{self.slot} {msg}", flush=True)

    # ------------------------------------------------------------------ setup
    def init_world(self):
        me = self.me = cambc.me()
        self.ctype = int(me.champion_name())
        self.arange = me.attack_range()
        self.melee = self.arange <= 1
        self.W = game_map.width()
        self.H = game_map.height()
        walls, holes, altars = game_map.walls(), game_map.holes(), game_map.altars()
        self.aspawn = game_map.spawn_region(ALLY)
        self.espawn = game_map.spawn_region(ENEMY)
        self.static_block = walls | holes | altars | self.espawn
        self.spawn_xy = set((v.x, v.y) for v in self.aspawn.positions())
        self.altar_vecs = altars.positions()
        self.altar_xy = [(v.x, v.y) for v in self.altar_vecs]
        goals = Bitboard()
        for v in self.altar_vecs:
            goals.draw_chebyshev(v, cambc.constants.SUBMIT_GEMS_RANGE)
        self.altar_goals = goals & ~self.static_block
        self.df_spawn = DistanceField()
        self.df_spawn.compute(self.static_block, self.aspawn, Bitboard())
        self.df_altar = DistanceField()
        self.df_altar.compute(self.static_block & ~self.altar_goals, self.altar_goals, Bitboard())
        self.df_enemy = DistanceField()
        self.df_enemy.compute(self.static_block & ~self.espawn, self.espawn, Bitboard())
        self.df_me = DistanceField()
        self.df_path = DistanceField()
        self.scratch = Bitboard()

        # camp groups
        self.groups = []
        self.tile_idx = {}
        for gi, cg in enumerate(game_map.camp_groups()):
            g = Grp()
            g.i = gi
            g.cg = cg
            g.gems = cg.gem_value()
            g.gold = cg.gold_value()
            g.resp = cg.max_respawn_timer()
            xy, vec, kind = [], [], []
            for c in cg.camps():
                sp = c.spawn_position()
                xy.append((sp.x, sp.y))
                vec.append(sp)
                kind.append(int(c.kind()))
            n = len(xy)
            g.n = n
            g.xy = xy
            g.xyset = set(xy)
            g.vec = vec
            g.v0 = vec[0]
            g.kind = kind
            g.hp = [T.camp_hp(k) for k in kind]
            g.st = [T.camp_st(k) for k in kind]
            g.stsum = sum(g.st)
            g.hp_total = sum(g.hp)
            g.hpsum = g.hp_total
            g.adj = [[i != j and cheb(xy[i][0], xy[i][1], xy[j][0], xy[j][1]) <= 1 for j in range(n)]
                     for i in range(n)]
            g.bb = cg.spawn_tiles()
            g.cx = sum(p[0] for p in xy) / n
            g.cy = sum(p[1] for p in xy) / n
            g.d_spawn = self.df_spawn.get(vec[0])
            d_enemy = self.df_enemy.get(vec[0])
            g.d_enemy = d_enemy
            g.far = d_enemy < g.d_spawn
            # groups the enemy reaches as fast as we do are fought over: discount them (1.0 safe .. 0.75 theirs)
            lean = (g.d_spawn - d_enemy + 6) / 12.0
            g.contest = 1.0 - 0.25 * (0.0 if lean < 0.0 else (1.0 if lean > 1.0 else lean))
            # contesting weight for the enemy's half (applied only when we are pushing, i.e. with company)
            lean2 = 1.0 - d_enemy / SIEGE_D
            g.deny = 1.0 + DENY_W * (0.0 if lean2 < 0.0 else (1.0 if lean2 > 1.0 else lean2))
            g.ready_at = 0
            g.fails = 0
            g.block_until = 0
            g.hpleft = None
            g.hp_ver = 0
            g.est_key = None
            g.est = {}
            for ci, t in enumerate(xy):
                self.tile_idx[t] = (gi, ci)
            self.groups.append(g)
        # monoliths are only worth the lives when nothing else on the map pays out gems (00contest)
        self.mono_ok = not any(2 in g.kind for g in self.groups)
        # canonical rank: nearest our spawn first (identical for every one of our champions)
        order = sorted(range(len(self.groups)), key=lambda i: self.groups[i].d_spawn)
        self.rank_to_g = order
        for r, gi in enumerate(order):
            self.groups[gi].rank = r
        self.near_groups = [self.groups[gi] for gi in order[:NEAR_K]]
        self.rest_groups = [self.groups[gi] for gi in order[NEAR_K:]]

        # team slot from the draft memory body order
        mem = bytes(cambc.DraftMemoryBuffer())
        self.slot = self.ctype % 6
        self.nslots = 6
        self.slot_types = []
        if len(mem) > 1 and mem[0] > 0:
            n = mem[0]
            types = list(mem[1:1 + n])
            if self.ctype in types and n <= 6:
                self.slot = types.index(self.ctype)
                self.nslots = n
                self.slot_types = types
            elif n < 6:
                self.slot = n + self.ctype % (6 - n)
        self.sbuf = bytearray(8)
        self.my_byte = 0
        self.rep_gems = 0
        self.tot = 0
        self.others = {}
        self.claims = {}
        self.last_done = {}
        self.done_rank = -1
        self.cstate = C_HEAD

        self.home_open = (self.nslots == HOME_OPEN_SLOTS)
        # corners (71x71, 5 bodies, 5 altars): seat A's bags deadlocked under threat until
        # they braved it; seat B fed deaths doing so. Seat from the lexicographically first
        # spawn tile (v18's method). Both policies verified by 18-game evals.
        sp = sorted(self.spawn_xy)
        ep = sorted((v.x, v.y) for v in self.espawn.positions())
        self.seat_a = bool(sp) and bool(ep) and sp[0] < ep[0]
        self.corner_map = (self.W, self.H, self.nslots) == (71, 71, 5) and len(self.altar_xy) == 5
        # seat A: bank under threat at threshold and never fight on bank runs (1050-358);
        # seat B: keep the strict gates and normal bank-run fights (974-358-phase-2 win)
        self.corner_seat = ("A" if self.seat_a else "B") if self.corner_map else None
        self.bank_run_fight = self.corner_seat != "A"
        self.espawn_xy = set((v.x, v.y) for v in self.espawn.positions())
        exs = [v.x for v in self.espawn.positions()]
        eys = [v.y for v in self.espawn.positions()]
        self.ehome_cx = sum(exs) // len(exs) if exs else self.W // 2
        self.ehome_cy = sum(eys) // len(eys) if eys else self.H // 2
        # how far each altar leans toward the enemy (positive: closer to them than to us)
        self.alt_lean = []
        for (ax, ay) in self.altar_xy:
            best_s, best_e = 65535, 65535
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    v = Vec2(ax + dx, ay + dy)
                    try:
                        ds = self.df_spawn.get(v)
                        de = self.df_enemy.get(v)
                    except Error:
                        continue
                    if ds < best_s:
                        best_s = ds
                    if de < best_e:
                        best_e = de
            self.alt_lean.append((best_s - best_e) if best_s < 65535 and best_e < 65535 else 0)
        # scheduling priority: tanks soak camp aggro first, healers act last
        try:
            prio = 10 if self.ctype == T.WILLOW else (100 if T.CHAMP.get(self.ctype, (0,))[0] >= 1000 or (self.ctype not in T.CHAMP and self.me.max_health() >= 1000) else 50)
            me.set_priority(prio)
        except Error:
            pass

        # abilities
        self.abil = []
        for ab in me.abilities():
            info = ab.info()
            kind = K_NONE
            for cls, k in _ABILITY_CLASSES:
                if type(info) is cls:
                    kind = k
                    break
            if kind == K_NONE:
                for cls, k in _ABILITY_CLASSES:
                    if isinstance(info, cls):
                        kind = k
                        break
            self.abil.append((ab, kind, info))
        self.has_dmg_spell = any(k in (K_DAMAGE, K_CC, K_AOE) and getattr(i, "modifier", 0) > 0
                                 for _, k, i in self.abil)

        # shopping: the advisor's data-driven build for this champion and map size when draftdata.py
        # has one, else the hand-written default (tanks melee fighters first, Stellar wants Focus, Willow supports)
        self.sig = f"{self.W}x{self.H}"
        self.build = T.build_for(self.ctype, self.sig, self.melee)
        self.set_pos = 0
        self.set_cnt = 0
        self.flags = set()
        self.field_shop = self.ctype == T.TAVI
        self.items = {}

        # per-turn / progress state
        self.stats_version = 0
        self.stats_dirty = True
        self.sightings = []          # (round, x, y) of recent enemy champion sightings
        self.last_attack_round = -10
        self.awake = False
        self.awake_round = -10
        self.took_round = -10
        self.took_amt = 0.0
        self.fight_gi = -1
        self.prev_eff = None
        self.camp_hp = {}            # (x, y) -> health when last seen
        self.camp_awake = {}         # (x, y) -> last round the camp lost health or hit us
        self.safe_round = -1         # round for which df_path holds the way out of camp reach
        self.safe_steps = None
        self.pair = self.ctype in (T.CASSI, T.ISSAC)
        self.margin = (1.8 if self.pair else 1.0) * X.CAMP_MARGIN    # Cassi and Issac die together: leave more room
        self.was_alive = True
        self.fuel_max = 0
        self.last_pos = None
        self.same_pos = 0
        self.pos_hist = []
        self.t_start = 0
        self.recall_started = -10
        self.cap_since = 0
        self.cap_tile_n = 0          # consecutive turns spent on the capture tiles
        self.cap_last = -10
        self.cw_n = 0
        self.kparty = K_PARTY
        self.revealed = False
        self.bank_latch = -1
        self.allies = []
        self.camps = []
        self.enemies = []
        self.inc = 0.0
        self.nearest_camp = 99
        self.shield_until = -1
        self.moved = False
        self.can_move = False
        self.can_attack = False
        self.can_cast = False
        self.inited = True

    def item(self, fam, stat):
        key = (fam, stat)
        it = self.items.get(key)
        if it is None:
            it = getattr(cambc.ItemType, f"{T.FAMILY[fam]}_SHARD_OF_{T.STAT[stat]}")
            self.items[key] = it
        return it

    def refresh_stats(self):
        me = self.me
        s = me.strength()
        f = me.focus()
        self.mstr, self.mfocus, self.maxhp = s, f, me.max_health()
        mon = 1.1 if "MON" in self.flags else 1.0
        dps = s * mon
        shield = 0.0
        for ab, kind, info in self.abil:
            cd = info.max_cooldown if info is not None else 8
            if kind in (K_DAMAGE, K_CC, K_AOE):
                dps += info.modifier * f * mon / (cd + 1)
            elif kind == K_RESET:
                dps += s * mon / (cd + 1)
            elif kind == K_SHIELD:
                shield = info.modifier * f
        self.dps = dps
        pvp = float(s)
        for ab, kind, info in self.abil:
            cd = info.max_cooldown if info is not None else 8
            if kind in (K_DAMAGE, K_CC, K_AOE):
                pvp += info.modifier * f / (cd + 1)
            elif kind == K_RESET:
                pvp += s / (cd + 1)
        self.pvp_dps = pvp
        self.splash = s * mon if self.ctype == T.VARG else 0.0
        self.shield_amt = shield
        self.mult = (0.7 if "JUN" in self.flags else 1.0) * (0.9 if "DUR" in self.flags else 1.0)
        self.stats_version += 1
        self.stats_dirty = False

    # ------------------------------------------------------------------ shared memory
    def read_shared(self):
        buf = self.sbuf
        cambc.shared_memory.read(0, buf)
        self.my_byte = buf[self.slot]
        others = {}
        for s in range(self.nslots):
            if s != self.slot:
                b = buf[s]
                r = (b & 63) - 1
                if r >= 0:
                    st = b >> 6
                    others[s] = (r, st)
                    if st == C_DONE and self.last_done.get(s) != r:
                        self.last_done[s] = r
                        g = self.groups[self.rank_to_g[r]] if r < len(self.rank_to_g) else None
                        if g is not None:
                            g.ready_at = max(g.ready_at, self.rnd + g.resp)
                            g.hpleft = None
                            g.hpsum = g.hp_total
                            g.hp_ver += 1
                else:
                    self.last_done.pop(s, None)
        self.others = others
        claims = {}
        for sl, (r, st) in others.items():
            if st != C_DONE:
                if r in claims:
                    claims[r].append((sl, st))
                else:
                    claims[r] = [(sl, st)]
        self.claims = claims
        self.tot = buf[6] | (buf[7] << 8)

    def write_shared(self, alive=True):
        b = 0
        if alive:
            if self.task is not None and self.task.rank < 63:
                b = ((C_STAGE if self.cap_since > 0 else self.cstate) << 6) | (self.task.rank + 1)
            elif self.done_rank >= 0 and self.done_rank < 63:
                b = (C_DONE << 6) | (self.done_rank + 1)
        if b != self.my_byte:
            cambc.shared_memory[self.slot] = b
            self.my_byte = b
        gems = self.gems if alive else 0
        if gems != self.rep_gems:
            tot = self.tot + gems - self.rep_gems
            if tot < 0:
                tot = 0
            if tot > 65535:
                tot = 65535
            cambc.shared_memory.write(6, bytes((tot & 255, tot >> 8)))
            self.tot = tot
            self.rep_gems = gems

    # ------------------------------------------------------------------ choosing work
    def group_estimate(self, g, n=1):
        """(turns to clear, damage the tank takes) for n champions of our strength working together."""
        key = self.stats_version * 1000003 + g.hp_ver
        if g.est_key != key:
            g.est_key = key
            g.est = {}
        est = g.est.get(n)
        if est is not None:
            return est
        hps = g.hp if g.hpleft is None else [h for h in g.hpleft]
        idx = [i for i in range(g.n) if hps[i] > 0.0]
        dps = self.dps * n
        if not idx:
            est = (0.0, 0.0)
        elif len(idx) > 5:
            tot = sum(hps[i] for i in idx)
            t = tot / max(dps, 1.0)
            est = (t, sum(g.st[i] for i in idx) * t * 0.5 * self.mult)
        else:
            sub_adj = [[g.adj[i][j] for j in idx] for i in idx]
            est = simulate([hps[i] for i in idx], [g.st[i] for i in idx], sub_adj, dps, self.splash, self.mult)
        g.est[n] = est
        return est

    def cycles_for(self, taken, b0, bfull):
        """Recall trips needed to absorb `taken` damage with a health budget of b0 (then bfull per trip)."""
        if taken <= b0:
            return 0
        return 1 + int((taken - b0) / bfull) if bfull > 0 else 9

    def choose_task(self):
        roots = Bitboard()
        roots.set(self.pos)
        self.df_me.compute(self.static_block, roots, self.scratch)
        best = self.best_group(self.near_groups)
        if best is None and self.rest_groups:
            best = self.best_group(self.rest_groups)
        return best

    def best_group(self, groups):
        """Pick the group with the best contest-weighted (gems + gold) per turn. A cheap bound first,
        an exact fight estimate only for the groups that could still win. Groups a teammate is already
        on are considered as joins when working together saves more time than it costs."""
        rnd = self.rnd
        claims = self.claims
        kappa = max(KAPPA_MIN, KAPPA0 - rnd / KAPPA_DECAY)
        hp_full = self.maxhp
        shield = self.shield_amt
        b0 = 0.7 * (self.hp + shield)
        bfull = 0.7 * (hp_full + shield)
        recall_t = 3 if "HOME" in self.flags else 5
        mult = self.mult
        cap_inc = 0.15 * hp_full / mult if mult > 0 else 1e9      # skip groups hitting harder than 15% hp/turn
        dps = self.dps if self.dps > 1.0 else 1.0
        spl = self.splash
        df_get = self.df_me.get
        cands = []
        joins = []
        party = K_PARTY
        big_ok = self.nslots >= BIG_PARTY
        # how hard to contest the enemy's half right now: needs push (gem race) and company
        esc = 0
        px, py = self.px, self.py
        for a in self.allies:
            if cheb(px, py, a[0], a[1]) <= ESCORT_R:
                esc += 1
        opening = self.home_open and rnd < OPEN_HOME_R
        push = 0.0 if opening else self.push_f * (1.0 if esc >= 2 else (0.5 if esc == 1 else 0.0))

        def weight(g):
            w = g.contest * (1.0 + (g.deny - 1.0) * push)
            return w if w > 0.05 else 0.05

        for g in groups:
            if g.block_until > rnd:
                continue
            if X.USE_MONO_VETO and not self.mono_ok and 4 in g.kind:
                continue
            if g.d_enemy <= ESP_AVOID and push > 0.3:
                continue                        # never camp on the enemy's doorstep (they spawn-heal there)
            hs = g.hpsum
            big = big_ok and is_big(g)
            cl = claims.get(g.rank)
            if cl is not None:
                if len(cl) < (BIG_PARTY if big and party < BIG_PARTY else party):
                    joins.append((g, cl))
                continue
            if g.stsum > cap_inc:
                continue
            if X.PARTY_FRAC > 0.0 and esc < X.PARTY_MIN and mult > 0 and g.stsum > X.PARTY_FRAC * hp_full / mult:
                continue                        # too dangerous to open alone: wait for company
            d = df_get(g.v0)
            if d >= UNREACHED:
                continue
            travel = d - 1 if d > 1 else 0
            wait = g.ready_at - rnd - travel
            if wait < 0:
                wait = 0
            elif wait > WAIT_NEAR_MAX:
                continue
            val = g.gems + kappa * g.gold
            ub = weight(g) * val / (travel + wait + CAP_TURNS + hs / (dps + spl * (g.n - 1)))
            cands.append((ub, g.i, travel, val))
        cands.sort(reverse=True)
        groups_by_i = self.groups
        estimate = self.group_estimate
        best = None
        best_score = 0.0
        best_join = None
        for ub, gi, travel, val in cands:
            if ub <= best_score:
                break
            g = groups_by_i[gi]
            # a group we can see right now may be respawning (taken by someone else): trust the engine
            timer = self.peek_respawn(g)
            if timer is not None:
                g.ready_at = rnd + timer
            wait = g.ready_at - rnd - travel
            if wait < 0:
                wait = 0
            elif wait > WAIT_NEAR_MAX:
                if timer is not None:
                    g.block_until = rnd + timer - WAIT_NEAR_MAX // 2
                continue
            tf, taken = estimate(g)
            if tf <= 0.0 and g.hpsum > 0.0:
                continue
            score = weight(g) * val / (travel + wait + CAP_TURNS + tf
                                       + self.cycles_for(taken, b0, bfull) * (recall_t + g.d_spawn + 3))
            for t, ex, ey in self.sightings:
                if rnd - t < 40 and cheb(ex, ey, int(g.cx), int(g.cy)) <= 9:
                    score *= 1.25 if (esc >= 1 and not opening) else 0.7  # hunt in company, keep away alone
                    break
            if score > best_score:
                best_score = score
                best = g
        # joins: a teammate is already on the group; is our help worth more than our time?
        if joins:
            r_ref = best_score if best_score > 0.0 else 0.3
            join_score = 0.0
            for g, cl in joins:
                if cl[0][1] == C_STAGE:
                    continue                            # they are already collecting the reward
                n0 = len(cl)
                d = df_get(g.v0)
                if d >= UNREACHED:
                    continue
                travel = d - 1 if d > 1 else 0
                tf0, tk0 = estimate(g, n0)
                tf1, tk1 = estimate(g, n0 + 1)
                if tf0 <= 0.0 or tf1 <= 0.0:
                    continue
                t_cycle = recall_t + g.d_spawn + 3
                big = big_ok and is_big(g)
                if big:
                    # the party shares the hits (nearest champion), so its health is pooled between trips
                    big0 = tf0 + self.cycles_for(tk0, n0 * b0, n0 * bfull) * t_cycle
                    big1 = tf1 + self.cycles_for(tk1, (n0 + 1) * b0, (n0 + 1) * bfull) * t_cycle
                else:
                    big0 = tf0 + self.cycles_for(tk0, b0, bfull) * t_cycle
                    big1 = tf1 + self.cycles_for(tk1, b0, bfull) * t_cycle
                saving = n0 * (big0 - big1)
                cost = travel + big1 + CAP_TURNS
                if big and (n0 + 1) * big1 + travel <= BIG_EFF * n0 * big0:
                    # our own share of the reward, as long as the party does not waste much champion-time
                    share = weight(g) * (g.gems + kappa * g.gold) / (n0 + 1) / cost
                    if share > join_score:
                        join_score = share
                        best_join = g
                if saving <= 1.15 * cost:
                    continue
                score = r_ref * saving / cost
                if score > join_score:
                    join_score = score
                    best_join = g
            if join_score > best_score:
                best = best_join
        return best

    def peek_respawn(self, g):
        """Respawn timer of a group we can see right now, None if it is not visibly respawning."""
        if cheb(self.px, self.py, int(g.cx), int(g.cy)) > 8:
            return None
        try:
            state = g.cg.state()
        except Error:
            return None
        if isinstance(state, game_map.CampGroupRespawning):
            return state.timer
        return None
