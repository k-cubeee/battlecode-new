"""Champion PvP: threat gates, a bounded turn planner, wards, altar locks, ganks and capture denial.

Adapted from v18's fight planner (ball.py plan_fight) with stricter engagement gates
(R_GO = 1.2 instead of 1.05) since its micro is battle-tested, plus interference it
does not have to defend against: ganks on rich isolated farmers, altar-lock cancels
of tribute channels, and stepping into camp captures to reset their timers.
"""
import cambc
from cambc import game_map
import tables as T
import tactics as X
from tactics import unit_dps
from world import (cheb, M_HOME, M_BANK, M_IDLE, C_FIGHT,
                   K_RESET, K_DAMAGE, K_AOE, K_CC, K_HEAL, K_DASH, K_SHIELD)

ALLY = cambc.Team.ALLY
ENEMY = cambc.Team.ENEMY
Vec2 = cambc.Vec2
Bitboard = cambc.Bitboard
DistanceField = cambc.DistanceField
Error = cambc.Error

# ------------------------------------------------------------------ tuning
SEE_R = 10                # enemies this close take part in the fight assessment
PATH_R = 7                # an enemy this many steps away counts as able to reach us
R_GO = 1.2                # strength ratio needed to start a fight
R_STAY = 0.7              # ... and to keep fighting
LAMBDA = 0.45             # weight on expected incoming damage in the turn planner
CO_W = 5.0                # pull toward our group's centre
ADV_W = 14.0              # pull toward the target while advancing
RET_W = 10.0              # pull toward the spawn while retreating
HEAL_W = 1.0
KILL_TURNS = 5.0
GEM_K = 8.0               # kill value per gem the enemy is carrying (the killer takes them)
GEM_F = 0.25              # focus-target score per carried gem (capped at 150)
HOME_FIGHT_R = 10         # on 4-champion maps, within this walk of our spawn the needed ratio ...
HOME_FIGHT_F = 0.85       # ... is scaled by this
HOME_R = 3                # enemies this close to their own spawn are not chased
HOLD_FIGHT_MAX = 4        # consecutive idle fight turns before we get on with other business
XCH_A = 0.4               # smoothing of the observed damage exchange in a fight
XCH_MIN_TURNS = 2         # turns of exchange seen before trusting it
XCH_MARGIN = 1.1          # back off when we would die before killing their weakest by this factor
BAIL_HP = 0.3             # back off below this health fraction unless a kill is at hand
DEEP_DE = 10              # alone, tiles this close to the enemy spawn are penalised ...
DEEP_W = 8.0              # ... by this much per tile
GEM_CAUTION = 160.0       # carried gems raise the strength ratio we want before fighting (x2 at this many)
BANK_NEAR = 3             # an altar this close is used even with enemies about
BANK_NEAR_GEMS = 20
BANK_RUN_GEMS = 40        # on a bank trip with this many gems a retreat heads for the altar when it is nearer
RET_W_FLEE = 60.0         # pull toward the spawn when running there would save us
FLEE_SAFETY = 0.8         # ... i.e. when the walk takes under this fraction of the turns we can survive
REGROUP_W = 3.0           # alone and outnumbered: drift toward the spawn instead of holding in place
COMMIT_GO_SLACK = 1       # in a camp fight, ignore enemies we would beat until they are this close
COMMIT_RUN_SLACK = 3      # ... and enemies we would lose to until this close
SPAWN_HOLD_D = 9          # on our spawn, enemies this close and a fight we would lose: wait for company
SPAWN_HOLD_MAX = 30       # ... for at most this many consecutive turns
SPAWN_HOLD_W = 1000.0     # turn-planner penalty for stepping off the spawn while holding
SPAWN_RISK_F = 0.35       # incoming damage on our spawn tiles counts this much (they heal us every turn end)
GANK_GEMS = 18            # a visible enemy carrying at least this many gems is worth a hunt
GANK_ISO_R = 12           # ... when no enemy ally stands this close to it
GANK_HAVE_R = 10          # ... and we have at least two champions (us included) this close to it
GANK_MIN_RATIO = 0.8      # gank only when the local strength ratio is at least this
MELEE_TYPES = T.MELEE_BODIES
WIN_MARGIN = 0.12         # to start a fight we must expect to finish it with at least this health fraction left
WIN_MARGIN_GEMS = 0.3     # ... plus this much more when carrying GEM_CAUTION gems (they drop to the killer)
CORNER_TURNS = 1.0        # cornered: fleeing home takes longer than this many times what we survive -> fight back
RECALL_SIGHT_AGE = 3      # an enemy seen this recently and now out of view still counts as close for recalls


class PvpMixin:
    # ------------------------------------------------------------------ setup
    def pvp_init(self):
        self.engaged = False
        self.hold_fights = 0
        self.skip_fight_until = -1
        self.last_ratio = 1.0
        self.hard_retreat = False
        self.xch_round = -10
        self.xch_hp = {}
        self.xch_t = 0.0
        self.xch_d = 0.0
        self.xch_n = 0
        self.replanned = False
        self.push_f = 0.5
        self.spawn_hold = False
        self.hold_sp = 0
        self.has_lock = False
        self.lock_type = None
        try:
            self.lock_type = cambc.ConsumableType.ALTAR_LOCK
        except Exception:
            pass
        self.df_near = DistanceField()

    # ------------------------------------------------------------------ push pressure
    def update_push(self):
        """How hard to contest the enemy's half: all out when they lead the gem race, economy first when we do."""
        try:
            ours = cambc.global_submitted_gems(ALLY)
            theirs = cambc.global_submitted_gems(ENEMY)
        except Exception:
            return
        f = 0.5 + (theirs - ours) / 200.0
        self.push_f = 0.0 if f < 0.0 else (1.0 if f > 1.0 else f)

    def escort_weight(self):
        """How much company we have: solo farmers do not contest deep, hunting parties do."""
        n = 0
        px, py = self.px, self.py
        for a in self.allies:
            if cheb(px, py, a[0], a[1]) <= 6:
                n += 1
        if n >= 2:
            return 1.0
        return 0.5 if n == 1 else 0.0

    # ------------------------------------------------------------------ wards
    def maybe_place_ward(self):
        """Every charged turn: drop the ward toward where we are going next (free, no move cost)."""
        me = self.me
        try:
            if me.ward_cooldown() != 0:
                return ""
            tiles = me.ward_placement_tiles()
        except Error:
            return ""
        if tiles.is_empty():
            return ""
        tx, ty = self.ward_point()
        best = None
        best_d = 99
        for v in tiles.positions():
            d = cheb(v.x, v.y, tx, ty)
            if d < best_d:
                best_d = d
                best = v
        if best is None:
            return ""
        try:
            if me.place_ward(best):
                self.log(f"WARD ({best.x},{best.y})")
                return "ward "
        except Error:
            pass
        return ""

    def ward_point(self):
        """A point within two tiles of us: the nearest enemy when we see one, else our next task,
        else an altar nearby, else the enemy half of the map."""
        px, py = self.px, self.py
        gx, gy = px, py
        if self.enemies:
            e = min(self.enemies, key=lambda e: cheb(px, py, e[0], e[1]))
            gx, gy = e[0], e[1]
        elif self.task is not None and not (self.cstate == C_FIGHT and self.camps):
            gx, gy = int(self.task.cx), int(self.task.cy)
        else:
            best = None
            best_d = 99
            for k, (ax, ay) in enumerate(self.altar_xy):
                d = cheb(px, py, ax, ay)
                if d < best_d:
                    best_d = d
                    best = (ax, ay)
            if best is not None and best_d <= 10:
                gx, gy = best
            else:
                gx, gy = self.ehome_cx, self.ehome_cy
        dx = gx - px
        dy = gy - py
        if dx > 2:
            dx = 2
        elif dx < -2:
            dx = -2
        if dy > 2:
            dy = 2
        elif dy < -2:
            dy = -2
        return px + dx, py + dy

    # ------------------------------------------------------------------ altar locks
    def sync_lock(self):
        try:
            if self.me.consumables()[int(self.lock_type)].count() > 0:
                self.has_lock = True
        except Error:
            pass

    def maybe_lock(self):
        """Place a carried lock on an altar the enemy is tributing at, on their side of the map,
        or on ours when they have been prowling around it."""
        if not self.has_lock or self.lock_type is None:
            return ""
        px, py = self.px, self.py
        rnd = self.rnd
        for k, (ax, ay) in enumerate(self.altar_xy):
            if cheb(px, py, ax, ay) > 2:
                continue
            v = self.altar_vecs[k]
            try:
                if cambc.vision.has_altar_lock(v):
                    continue
            except Error:
                continue
            want = False
            for e in self.enemies:
                if (e[10] & 4) and cheb(e[0], e[1], ax, ay) <= 1:
                    want = True                     # cancel a tribute in progress
                    break
            if not want and self.alt_lean[k] >= -2 and self.push_f >= 0.5:
                want = True                         # deny the enemy's side of the map
            if not want and self.alt_lean[k] < 0:
                for t, sx, sy in self.sightings:
                    if rnd - t <= 20 and cheb(sx, sy, ax, ay) <= 8 and self.push_f >= 0.55:
                        want = True                 # they are prowling around our altar: protect it
                        break
            if want:
                try:
                    cons = self.me.consumables()[int(self.lock_type)]
                    if cons.use(v):
                        self.has_lock = False
                        self.log(f"LOCK altar {k}")
                        return "lock "
                except Error:
                    pass
        return ""

    def break_lock(self):
        """Hit an enemy lock on an altar we stand by (three hits remove it), unless enemies are on top of us."""
        if not self.can_attack:
            return ""
        px, py = self.px, self.py
        for e in self.enemies:
            reach = 2 if e[6] <= 1 else e[6]
            if cheb(px, py, e[0], e[1]) <= reach + 1:
                return ""                      # do not feed the guards: fight or flee first
        try:
            locks = cambc.vision.altar_locks(ENEMY)
        except Error:
            return ""
        for lk in locks:
            try:
                p = lk.position()
            except Error:
                continue
            if cheb(px, py, p.x, p.y) <= self.arange:
                try:
                    self.me.attack(lk)
                    self.can_attack = False
                    if not self.melee:
                        self.can_move = False
                    self.log("break-lock")
                    return "break-lock "
                except Error:
                    return ""
        return ""

    # ------------------------------------------------------------------ perception helpers
    def fight_ratio(self, near):
        """(our strength x hp) / (their strength x hp) for the enemies that can engage us."""
        px, py = self.px, self.py
        a_hp = self.hp + self.shield_value()
        a_dps = unit_dps(self.ctype, self.mstr, self.mfocus)
        if self.ctype == T.WILLOW:
            a_hp += 2.0 * self.mfocus
        for a in self.allies:
            if cheb(px, py, a[0], a[1]) <= SEE_R and self.ally_in_fight(a, near):
                a_hp += a[2]
                a_dps += unit_dps(a[6], a[4], a[5])
                if a[6] == T.WILLOW:
                    a_hp += 2.0 * a[5]
        e_hp = 0.0
        e_dps = 0.0
        for e in near:
            w = 3.0 if (e[0], e[1]) in self.espawn_xy else 1.0
            e_hp += e[2] * w
            e_dps += unit_dps(e[8], e[3], e[7])
        if e_hp < 1.0 or e_dps < 1.0:
            return 9.0
        return (a_hp * a_dps) / (e_hp * e_dps)

    def engage_sim(self, near):
        """Brawl simulation with everybody who can take part: (win, our health left). The bot itself must survive."""
        px, py = self.px, self.py
        my_hp = self.hp + self.shield_value()
        if self.ctype == T.WILLOW:
            my_hp += 2.0 * self.mfocus
        mine = [(my_hp, unit_dps(self.ctype, self.mstr, self.mfocus))]
        for a in self.allies:
            if cheb(px, py, a[0], a[1]) <= SEE_R and self.ally_in_fight(a, near):
                hp = a[2] + (2.0 * a[5] if a[6] == T.WILLOW else 0.0)
                mine.append((hp, unit_dps(a[6], a[4], a[5])))
        foes = []
        for e in near:
            w = X.SPAWN_HEAL_W if (e[0], e[1]) in self.espawn_xy else 1.0
            foes.append((e[2] * w, unit_dps(e[8], e[3], e[7])))
        burst = 0.0
        if self.can_cast:
            for ab, kind, info in self.abil:
                if kind in (K_DAMAGE, K_AOE, K_CC) and info.modifier > 0 and ab.cooldown() == 0:
                    burst += info.modifier * self.mfocus
        win, left, _ = X.fight_sim(mine, foes, burst, self.inc)
        return win, left

    def win_margin(self):
        """Health fraction we want left at the end of a fight we start (less to keep one going)."""
        m = 0.0 if self.engaged else WIN_MARGIN
        if self.gems > 0:
            g = self.gems / GEM_CAUTION
            m += WIN_MARGIN_GEMS * (1.0 if g > 1.0 else g)
        return m

    def cornered(self, rel):
        """Running will not work: a melee champion is on us and home is further than we would live."""
        for ab, kind, info in self.abil:
            if kind == K_DASH and ab.cooldown() == 0 and self.can_cast:
                return False
        chasers = [e for e in rel if e[6] <= 1 and cheb(self.px, self.py, e[0], e[1]) <= 2]
        if not chasers or self.hp <= 0:
            return False
        dps = sum(unit_dps(e[8], e[3], e[7]) for e in chasers)
        survive = (self.hp + self.shield_value()) / max(dps, 1.0)
        home = min(self.df_spawn.get(self.pos), self.df_altar.get(self.pos) if self.gems >= BANK_RUN_GEMS else 65535)
        return home > survive * CORNER_TURNS

    def dash_home(self):
        """Hurt, with an enemy about to catch us: use a ready dash to open the gap, toward home when we can."""
        if not self.can_cast or not self.enemies:
            return False
        if self.hp + self.shield_value() >= self.maxhp * 0.6:
            return False
        dash = None
        for ab, kind, info in self.abil:
            if kind == K_DASH and ab.cooldown() == 0:
                dash = ab
        if dash is None:
            return False
        px, py = self.px, self.py
        if min(X.hit_turn(cheb(px, py, e[0], e[1]), e[6]) for e in self.enemies) > 2:
            return False
        block = self.dyn_block()
        W, H = self.W, self.H

        def free(x, y):
            return 0 <= x < W and 0 <= y < H and not block.is_set(Vec2(x, y))

        def gap_at(x, y):
            return min(cheb(x, y, e[0], e[1]) - X.reach_of(e[6]) for e in self.enemies)

        cur = gap_at(px, py)
        best = None
        best_key = 1e18
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                if max(abs(dx), abs(dy)) != 2:
                    continue
                nx, ny = px + dx, py + dy
                mx, my = px + (dx > 0) - (dx < 0), py + (dy > 0) - (dy < 0)
                if not free(nx, ny) or not free(mx, my):
                    continue
                gap = gap_at(nx, ny)
                if gap <= cur:
                    continue
                key = self.df_spawn.get(Vec2(nx, ny)) - 2.0 * gap
                if key < best_key:
                    best_key = key
                    best = (nx, ny)
        if best is None:
            return False
        try:
            if dash.use(Vec2(best[0], best[1])):
                self.px, self.py = best
                self.pos = Vec2(best[0], best[1])
                self.can_cast = False
                self.moved = True
                if not self.melee:
                    self.can_attack = False
                return True
        except Error:
            pass
        return False

    def ally_in_fight(self, a, near):
        """An ally that can take part: beside us, or within striking distance of one of these enemies."""
        if a[9] & (2 | 4 | 4096):
            return False                          # recalling, tributing or teleporting
        if cheb(self.px, self.py, a[0], a[1]) <= 2:
            return True
        reach = (1 if a[6] in MELEE_TYPES else 4) + 2
        for e in near:
            if cheb(a[0], a[1], e[0], e[1]) <= reach:
                return True
        return False

    def at_home(self, e):
        """An enemy loitering on (or next to) its own spawn heals every turn there."""
        if (e[0], e[1]) in self.espawn_xy:
            return True
        try:
            return self.df_enemy.get(e[5]) <= HOME_R
        except Error:
            return False

    def threat_near(self):
        """An enemy close enough to matter (enemies loitering on their own spawn do not count)."""
        px, py = self.px, self.py
        for e in self.enemies:
            d = cheb(px, py, e[0], e[1])
            if d <= 3 or (d <= 6 and not self.at_home(e)):
                return True
        return False

    def observe_exchange(self, rel):
        """(turns until we die, turns until their weakest dies) from the damage actually seen lately."""
        rnd = self.rnd
        cont = self.xch_round == rnd - 1
        hp_now = {}
        dealt = 0.0
        for e in rel:
            try:
                key = e[4].__index__()
            except Exception:
                key = (e[0], e[1])
            hp_now[key] = e[2]
            before = self.xch_hp.get(key)
            if cont and before is not None and before > e[2]:
                dealt += before - e[2]
        taken = self.took_amt
        if cont:
            self.xch_t = XCH_A * taken + (1.0 - XCH_A) * self.xch_t
            self.xch_d = XCH_A * dealt + (1.0 - XCH_A) * self.xch_d
            self.xch_n += 1
        else:
            self.xch_t = taken
            self.xch_d = 0.0
            self.xch_n = 0
        self.xch_hp = hp_now
        self.xch_round = rnd
        if self.xch_n < XCH_MIN_TURNS or self.xch_t < 1.0:
            return None
        eff = self.hp + self.shield_value()
        weakest = min(e[2] for e in rel)
        return eff / self.xch_t, weakest / max(self.xch_d, 1.0)

    def can_finish(self, rel):
        dmg = self.mstr * 1.1
        if self.can_cast:
            for ab, kind, info in self.abil:
                if kind in (K_DAMAGE, K_AOE, K_CC) and info.modifier > 0 and ab.cooldown() == 0:
                    dmg += info.modifier * self.mfocus
                    break
        reach = self.arange if self.arange > 4 else 4
        for e in rel:
            if cheb(self.px, self.py, e[0], e[1]) <= reach and e[2] <= dmg:
                return True
        return False

    # ------------------------------------------------------------------ spawn hold
    def want_spawn_hold(self):
        """On our spawn with enemies lurking and a fight we would lose: wait there for company."""
        if (self.px, self.py) not in self.spawn_xy:
            self.hold_sp = 0
            return False
        near = [e for e in self.enemies if cheb(self.px, self.py, e[0], e[1]) <= SPAWN_HOLD_D]
        if not near:
            self.hold_sp = 0
            return False
        if self.hold_sp >= SPAWN_HOLD_MAX:
            return False
        d = self.df_altar.get(self.pos)
        if d < 65535 and self.endgame_bank(d):
            return False
        if self.fight_ratio(near) >= R_GO:
            return False
        self.hold_sp += 1
        return True

    # ------------------------------------------------------------------ skirmish
    def skirmish(self):
        px, py = self.px, self.py
        self.replanned = False
        near = [e for e in self.enemies if cheb(px, py, e[0], e[1]) <= SEE_R]
        if not near:
            self.engaged = False
            self.hold_fights = 0
            return None
        # only enemies that can really get at us: within shooting distance, or a short walk away
        roots = Bitboard()
        roots.set(self.pos)
        self.df_near.compute(self.static_block, roots, self.scratch, PATH_R)
        rel = []
        for e in near:
            d = cheb(px, py, e[0], e[1])
            if d <= 4:
                rel.append(e)
            elif self.df_near.get(e[5]) <= PATH_R and not self.at_home(e):
                rel.append(e)
        if not rel:
            self.engaged = False
            return None
        if self.skip_fight_until > self.rnd:
            if not any(cheb(px, py, e[0], e[1]) <= (2 if e[6] <= 1 else e[6]) + 1 for e in rel):
                return None                       # resting from a stand-off, unless someone can hit us
        ratio = self.fight_ratio(rel)
        need = R_STAY if self.engaged else R_GO
        if self.gems > 0:
            need *= 1.0 + self.gems / GEM_CAUTION
        if self.home_open:
            # 4-champion maps: fight readily near home (healing, respawns)
            try:
                if self.df_spawn.get(self.pos) <= HOME_FIGHT_R:
                    need *= HOME_FIGHT_F
            except Error:
                pass
        # Only fight what we expect to beat: the strength ratio must clear the bar AND a turn-by-turn simulation
        # of everybody involved must end with the foes dead and enough of our health left.
        win, left = self.engage_sim(rel)
        sim_ok = (not X.USE_SIM_GATE) or (win and left >= self.win_margin() * self.maxhp)
        go = ratio >= need and sim_ok
        if self.mode == M_BANK and self.gems >= BANK_RUN_GEMS:
            if not self.bank_run_fight:
                go = False                      # corner seat A: bank runs never start fights
        elif not go:
            go = sim_ok and self.gank_go(rel)   # no gank-chasing while hauling a bag
        xch = self.observe_exchange(rel)
        eff = self.hp + self.shield_value()
        bad = False
        if go and xch is not None and xch[0] < xch[1] * XCH_MARGIN:
            go = False                            # the real exchange says we lose this one
            bad = True
        if go and eff < BAIL_HP * self.maxhp and not self.can_finish(rel):
            go = False
            bad = True
        self.engaged = go
        self.last_ratio = ratio
        self.hard_retreat = bad or ratio < 0.5 or eff < self.maxhp * 0.4 or (X.USE_SIM_GATE and not win)
        if X.USE_CORNER and not go and self.cornered(rel):
            go = True                             # no way out: hit back instead of dying with our back turned
            self.hard_retreat = False
            self.engaged = True
        if self.committed():
            slack = 99
            near_me = 99
            for e in rel:
                d = cheb(px, py, e[0], e[1])
                s_ = d - (2 if e[6] <= 1 else e[6])
                if s_ < slack:
                    slack = s_
                if d < near_me:
                    near_me = d
            if (go and slack >= COMMIT_GO_SLACK and near_me > 4) or (not go and slack >= COMMIT_RUN_SLACK):
                return None                       # nobody can get at us yet: keep working the camp
        if cambc.fuel_remaining() < 500000:
            return self.fallback_attack(rel)
        act = self.plan_fight(rel, not go)
        if act is None:
            return None
        if act.startswith("hold"):
            self.hold_fights += 1
            if self.hold_fights >= HOLD_FIGHT_MAX:
                self.skip_fight_until = self.rnd + 3    # nobody wants to move: carry on with the day's work
                self.hold_fights = 0
        else:
            self.hold_fights = 0
        return act

    def fallback_attack(self, near):
        if not self.can_attack:
            return None
        best = None
        for e in near:
            if cheb(self.px, self.py, e[0], e[1]) <= self.arange and (best is None or e[2] < best[2]):
                best = e
        if best is None:
            return None
        try:
            self.me.attack(best[4])
            self.can_attack = False
            if not self.melee:
                self.can_move = False
            return "hit-fallback"
        except Error:
            return None

    # ------------------------------------------------------------------ ganks
    def gank_target(self):
        """A visible enemy carrying a big bag, cut off from its team, that we can take with company."""
        px, py = self.px, self.py
        for e in self.enemies:
            if e[11] < GANK_GEMS:
                continue
            if self.at_home(e):
                continue
            iso = True
            for o in self.enemies:
                if o is not e and cheb(o[0], o[1], e[0], e[1]) <= GANK_ISO_R:
                    iso = False
                    break
            if not iso:
                continue
            n = 1 if cheb(px, py, e[0], e[1]) <= GANK_HAVE_R else 0
            for a in self.allies:
                if cheb(a[0], a[1], e[0], e[1]) <= GANK_HAVE_R and not (a[9] & (2 | 4 | 4096)):
                    n += 1
            if n >= 2:
                return e
        return None

    def gank_go(self, rel):
        """The strength gate is relaxed when the quarry on the table is a rich isolated farmer."""
        gt = self.gank_target()
        if gt is None:
            return False
        on_table = any((e[0], e[1]) == (gt[0], gt[1]) for e in rel)
        if not on_table:
            return False
        if self.fight_ratio(rel) < GANK_MIN_RATIO:
            return False
        my_n = 1
        for a in self.allies:
            if cheb(a[0], a[1], gt[0], gt[1]) <= GANK_HAVE_R and not (a[9] & (2 | 4 | 4096)):
                my_n += 1
        en_n = 1
        for e2 in self.enemies:
            if (e2[0], e2[1]) != (gt[0], gt[1]) and cheb(e2[0], e2[1], gt[0], gt[1]) <= GANK_ISO_R:
                en_n += 1
        return my_n >= en_n

    def gank_hunt(self):
        """Walk toward a gank quarry when it is out of fighting range."""
        if not self.enemies or not self.can_move or self.committed():
            return None
        if self.mode != M_IDLE:
            return None
        e = self.gank_target()
        if e is None:
            return None
        if cheb(self.px, self.py, e[0], e[1]) <= self.arange + 1:
            return None                          # skirmish will handle the kill from here
        goals = Bitboard()
        goals.draw_chebyshev(e[5], 2)
        goals &= ~self.dyn_block()
        if goals.is_empty():
            return None
        if self.step_to(goals):
            self.log(f"GANK -> ({e[0]},{e[1]}) gems={e[11]}")
            return "gank-hunt"
        return None

    # ------------------------------------------------------------------ capture denial
    def deny_capture(self):
        """An enemy collecting a camp reward: step onto the spawn tiles to reset their five-round timer."""
        if not self.enemies or not self.can_move:
            return None
        if self.mode != M_IDLE or self.committed():
            return None
        if self.hp < self.maxhp * 0.55:
            return None
        px, py = self.px, self.py
        en_near = 0
        for e in self.enemies:
            if cheb(px, py, e[0], e[1]) <= 10:
                en_near += 1
        if en_near >= 2:
            return None                          # do not dive into a group of them
        ally_n = 1
        for a in self.allies:
            if cheb(px, py, a[0], a[1]) <= 10 and not (a[9] & (2 | 4 | 4096)):
                ally_n += 1
        if ally_n < en_near:
            return None
        best = None
        seen_gi = set()
        for c in self.camps:
            gi = c[3]
            if gi in seen_gi:
                continue
            seen_gi.add(gi)
            if cheb(px, py, c[0], c[1]) > 8:
                continue
            g = self.groups[gi]
            try:
                state = g.cg.state()
            except Error:
                continue
            if not isinstance(state, game_map.CampGroupRewardAvailable):
                continue
            on = False
            for e in self.enemies:
                if (e[0], e[1]) in g.xyset:
                    reach = 2 if e[6] <= 1 else e[6]
                    if cheb(px, py, e[0], e[1]) <= reach + 1:
                        return None              # that farmer can hit us as we step in
                    on = True
            if on:
                best = g
                break
        if best is None:
            return None
        goals = best.bb & ~self.dyn_block()
        if goals.is_empty():
            return None
        if goals.is_set(self.pos):
            self.log(f"DENY hold {best.i}")
            return "deny-hold"
        if self.step_to(goals):
            self.log(f"DENY -> {best.i}")
            return "deny-cap"
        return None

    # ------------------------------------------------------------------ the turn planner
    def plan_fight(self, near, retreat):
        """Pick this turn's move, spell and attack by scoring every combination worth considering."""
        me = self.me
        px, py = self.px, self.py
        arange = self.arange
        melee = self.melee
        f_me = self.mfocus
        s_me = self.mstr
        rnd = self.rnd
        allies = self.allies
        near = sorted(near, key=lambda e: cheb(px, py, e[0], e[1]))[:5]
        n_en = len(near)
        EX = [e[0] for e in near]
        EY = [e[1] for e in near]
        EHP = [e[2] for e in near]
        EDPS = []
        EREACH = []
        EWT = []
        EKV = []
        ESV = []
        ESHARE = []
        mean_dps = sum(unit_dps(e[8], e[3], e[7]) for e in near) / max(1, len(near))
        for e in near:
            dps = unit_dps(e[8], e[3], e[7])
            EDPS.append(dps)
            reach = 2 if e[6] <= 1 else e[6]
            EREACH.append(reach)
            w = X.target_weight(e[8], e[2], e[9], dps, mean_dps, e[11])
            if (e[0], e[1]) in self.espawn_xy:
                w *= 0.15
            EWT.append(w)
            eg = e[11]
            EKV.append(dps * KILL_TURNS + 60.0 + eg * GEM_K)
            st = e[10]
            ESV.append(dps * 1.2 + (70.0 if st & 4 else 0.0) + (30.0 if st & 2 else 0.0))
            n = 1
            for a in allies:
                if cheb(a[0], a[1], e[0], e[1]) <= reach + 1:
                    n += 1
            ESHARE.append(n)
        # our group's centre
        cx = cy = None
        if allies:
            sx, sy, k = px, py, 1
            for a in allies:
                if cheb(px, py, a[0], a[1]) <= 10:
                    sx += a[0]
                    sy += a[1]
                    k += 1
            if k > 1:
                cx, cy = int(sx / k), int(sy / k)
        # the enemy we want to get to: weakest-looking, most valuable
        focus = -1
        fscore = -1e9
        for i in range(n_en):
            e = near[i]
            if cheb(px, py, e[0], e[1]) > arange + 1 and self.at_home(e):
                continue
            frac = e[2] / e[9] if e[9] > 0 else 1.0
            sc = EWT[i] * 30.0 + (1.0 - frac) * 40.0 - cheb(px, py, EX[i], EY[i]) * 0.5
            sc += (e[11] if e[11] < 150 else 150) * GEM_F
            if e[2] <= s_me + 100.0:
                sc += 40.0
            if sc > fscore:
                fscore = sc
                focus = i
        camp_thr = []
        awk = self.camp_awake
        for c in self.camps:
            st = self.groups[c[3]].st[c[4]]
            if st > 0.0 and rnd - awk.get((c[0], c[1]), -99) <= 2:
                camp_thr.append((c[0], c[1], st * 0.7))
        hpf = (self.hp + self.shield_value()) / max(1.0, self.maxhp)
        lam = LAMBDA
        if retreat:
            lam = 1.6
        elif hpf < 0.6:
            lam = LAMBDA * (1.0 + (0.6 - hpf) * 6.0)
        if self.maxhp >= 1000.0 and not retreat:
            lam *= 0.8
        if self.pair:
            lam *= 1.5
        if not retreat and self.last_ratio > 1.5:
            lam *= 1.5 / self.last_ratio             # clearly stronger than them: the exchange rate favours us
        # ---- candidate end positions (position, kind: 0 stay / 1 walk / 2 dash)
        block = self.dyn_block()
        W, H = self.W, self.H
        cands = [(px, py, 0, None)]
        if self.can_move:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx or dy:
                        nx, ny = px + dx, py + dy
                        if 0 <= nx < W and 0 <= ny < H and not block.is_set(Vec2(nx, ny)):
                            cands.append((nx, ny, 1, None))
        dash = None
        if self.can_cast and (melee or retreat):
            for ab, kind, info in self.abil:
                if kind == K_DASH and ab.cooldown() == 0:
                    dash = ab
        shield_ab = None
        if not self.shield_value() > 0.0:
            for ab, kind, info in self.abil:
                if kind == K_SHIELD and ab.cooldown() == 0:
                    shield_ab = (ab, info)
        close_enemy = any(cheb(px, py, EX[i], EY[i]) <= 5 for i in range(n_en))
        alone = not any(self.ally_in_fight(a, near) for a in allies)
        df_enemy = self.df_enemy
        no_chase = alone and focus >= 0 and df_enemy.get(near[focus][5]) < DEEP_DE + 4
        hard = retreat and self.hard_retreat
        adv_w = ADV_W * (1.0 + 0.5 * self.hold_fights)
        df_spawn = self.df_spawn
        splash = self.splash
        best = [-1e18, None]
        flee_w = RET_W
        if retreat and self.mode == M_BANK and self.gems >= BANK_RUN_GEMS and \
                self.df_altar.get(self.pos) < df_spawn.get(self.pos):
            df_spawn = self.df_altar               # on a bank trip: the altar is the nearer way out
        if retreat and hard:
            inc_est = self.xch_t if self.xch_n >= 1 else 0.0
            if inc_est < 1.0:
                inc_est = sum(EDPS[i] for i in range(n_en) if cheb(px, py, EX[i], EY[i]) <= EREACH[i] + 1)
            surv = (self.hp + self.shield_value()) / (inc_est if inc_est > 1.0 else 1.0)
            if df_spawn.get(self.pos) <= surv * FLEE_SAFETY:
                flee_w = RET_W_FLEE            # we can make it home, where the spawn heals us: go
        regroup = retreat and not hard and alone and close_enemy

        def tile_free(nx, ny):
            return 0 <= nx < W and 0 <= ny < H and not block.is_set(Vec2(nx, ny))

        spawn_set = self.spawn_xy
        hold = self.spawn_hold

        def evaluate(x, y, moved, dab):
            dist = [cheb(x, y, EX[i], EY[i]) for i in range(n_en)]
            risk = 0.0
            for i in range(n_en):
                if dist[i] <= EREACH[i]:
                    risk += EDPS[i] / ESHARE[i]
            for (kx, ky, kd) in camp_thr:
                if cheb(x, y, kx, ky) <= X.CAMP_REACH:
                    risk += kd
            on_sp = (x, y) in spawn_set
            if on_sp:
                risk *= SPAWN_RISK_F          # spawn tiles heal us to full every turn end, enemies can't step on
            pos_v = -lam * risk
            if hold and not on_sp:
                pos_v -= SPAWN_HOLD_W
            if cx is not None:
                dc = cheb(x, y, cx, cy) - 3
                if dc > 0:
                    pos_v -= CO_W * dc
            if retreat:
                if hard:
                    pos_v -= flee_w * df_spawn.get(Vec2(x, y))
                elif regroup:
                    pos_v -= REGROUP_W * df_spawn.get(Vec2(x, y))
            elif focus >= 0 and not no_chase:
                d = dist[focus] - arange
                if d > 0:
                    pos_v -= adv_w * d
            if alone:
                de = df_enemy.get(Vec2(x, y))
                if de < DEEP_DE:
                    pos_v -= DEEP_W * (DEEP_DE - de)
            atk_ok = self.can_attack and (melee or moved == 0)
            in_rng = [i for i in range(n_en) if dist[i] <= arange] if atk_ok else []
            opts = [(0.0, 0, None, None, None, None)]
            if dab is None and self.can_cast:
                for ab, kind, info in self.abil:
                    if ab.cooldown() != 0:
                        continue
                    if kind == K_RESET:
                        if in_rng:
                            opts.append((0.0, K_RESET, ab, info, None, None))
                    elif kind == K_SHIELD:
                        if shield_ab is not None and close_enemy:
                            opts.append((self.shield_amt * 0.5, K_SHIELD, ab, info, None, None))
                    elif kind == K_HEAL:
                        full = info.modifier * f_me
                        for a in allies:
                            if cheb(x, y, a[0], a[1]) <= info.range:
                                missing = a[3] - a[2]
                                amt = full if missing > full else missing
                                if amt > 0:
                                    frac = a[2] / a[3] if a[3] > 0 else 1.0
                                    opts.append((HEAL_W * amt + (120.0 if frac < 0.35 else 0.0), K_HEAL, ab, info, a, None))
                    elif kind == K_DAMAGE or kind == K_CC:
                        if info.exclusive_with_movement and moved:
                            continue
                        dmg = info.modifier * f_me
                        rng = info.range
                        for i in range(n_en):
                            if dist[i] <= rng:
                                real = dmg if dmg < EHP[i] else EHP[i]
                                v = real * EWT[i]
                                if dmg > 0 and dmg >= EHP[i]:
                                    v += EKV[i]
                                if kind == K_CC:
                                    v += ESV[i]
                                opts.append((v, kind, ab, info, i, {i: dmg}))
                    elif kind == K_AOE:
                        if info.exclusive_with_movement and moved:
                            continue
                        dmg = info.modifier * f_me
                        rng = info.range
                        rad = getattr(info, "radius", 1)
                        stun = getattr(info, "duration", 0) > 0
                        centres = set()
                        for i in range(n_en):
                            if dist[i] <= rng:
                                centres.add((EX[i], EY[i]))
                        for i in range(n_en):
                            for j in range(i + 1, n_en):
                                if cheb(EX[i], EY[i], EX[j], EY[j]) == 2:
                                    mx, my = (EX[i] + EX[j]) // 2, (EY[i] + EY[j]) // 2
                                    if cheb(x, y, mx, my) <= rng:
                                        centres.add((mx, my))
                        for (ccx, ccy) in centres:
                            hits = {}
                            val = 0.0
                            for i in range(n_en):
                                if cheb(ccx, ccy, EX[i], EY[i]) <= rad:
                                    real = dmg if dmg < EHP[i] else EHP[i]
                                    v = real * EWT[i]
                                    if dmg >= EHP[i]:
                                        v += EKV[i]
                                    if stun:
                                        v += ESV[i]
                                    hits[i] = dmg
                                    val += v
                            if hits:
                                opts.append((val, K_AOE, ab, info, Vec2(ccx, ccy), hits))
            gain = 0.0
            for (cval, ckind, cab, cinfo, ctgt, cmap) in opts:
                hpl = list(EHP)
                if cmap:
                    for i, d in cmap.items():
                        hpl[i] -= d
                av = 0.0
                atk_i = -1
                for i in in_rng:
                    h = hpl[i]
                    if h <= 0.0:
                        continue
                    real = s_me if s_me < h else h
                    v = real * EWT[i]
                    if s_me >= h:
                        v += EKV[i]
                    if splash > 0.0:
                        for j in range(n_en):
                            if j != i and hpl[j] > 0.0 and cheb(EX[i], EY[i], EX[j], EY[j]) <= 1:
                                v += (splash if splash < hpl[j] else hpl[j]) * EWT[j]
                    if v > av:
                        av = v
                        atk_i = i
                total = cval + av + pos_v
                atk2 = -1
                if ckind == K_RESET:
                    if atk_i < 0:
                        continue
                    best2 = 0.0
                    for i in in_rng:
                        hh = hpl[i] - (s_me if i == atk_i else 0.0)
                        if hh <= 0.0:
                            continue
                        real = s_me if s_me < hh else hh
                        v = real * EWT[i]
                        if s_me >= hh:
                            v += EKV[i]
                        if v > best2:
                            best2 = v
                            atk2 = i
                    total += best2
                if total - pos_v > gain:
                    gain = total - pos_v
                if total > best[0]:
                    best[0] = total
                    best[1] = (x, y, moved, dab, ckind, cab, cinfo, ctgt, atk_i, atk2)
            return gain

        got_attack = False
        for (x, y, moved, dab) in cands:
            if evaluate(x, y, moved, dab) > 0.0:
                got_attack = True
        # dashing in (or out): only tiles two steps away that let a melee champion strike, or that lead home
        if dash is not None and close_enemy and not (got_attack and not retreat):
            seen = set()
            if retreat:
                ring = []
                for dx in range(-2, 3):
                    for dy in range(-2, 3):
                        if max(abs(dx), abs(dy)) == 2:
                            ring.append((px + dx, py + dy))
                # best ways out: toward home and away from every enemy that can reach us
                def ring_key(t):
                    if not (0 <= t[0] < W and 0 <= t[1] < H):
                        return 1e9
                    away = min(cheb(t[0], t[1], EX[i], EY[i]) - EREACH[i] for i in range(n_en))
                    return df_spawn.get(Vec2(t[0], t[1])) - 2.0 * away
                ring.sort(key=ring_key)
                tiles = ring[:6]
            else:
                tiles = []
                for i in range(n_en):
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            tiles.append((EX[i] + dx, EY[i] + dy))
            for (nx, ny) in tiles:
                if (nx, ny) in seen:
                    continue
                seen.add((nx, ny))
                if cheb(px, py, nx, ny) != 2 or not tile_free(nx, ny):
                    continue
                mx, my = px + ((nx > px) - (nx < px)), py + ((ny > py) - (ny < py))
                if not tile_free(mx, my):
                    continue
                evaluate(nx, ny, 2, dash)
        if best[1] is None:
            return None
        return self.exec_plan(best[1], near, retreat)

    def exec_plan(self, plan, near, retreat):
        me = self.me
        x, y, moved, dab, ckind, cab, cinfo, ctgt, atk_i, atk2 = plan
        act = ""
        px, py = self.px, self.py
        failed_move = False
        if moved == 2 and dab is not None:
            try:
                if dab.use(Vec2(x, y)):
                    self.px, self.py = x, y
                    self.pos = Vec2(x, y)
                    self.can_cast = False
                    self.moved = True
                    if not self.melee:
                        self.can_attack = False
                    act += "dash "
                else:
                    failed_move = True
            except Error:
                failed_move = True
        elif moved == 1:
            try:
                if me.move(Vec2(x - px, y - py)):
                    self.px, self.py = x, y
                    self.pos = Vec2(x, y)
                    self.moved = True
                    self.can_move = False
                    if not self.melee:
                        self.can_attack = False
                    act += "step "
                else:
                    failed_move = True
            except Error:
                failed_move = True
        if failed_move:
            # an unseen unit sits on that tile: it is revealed now, so plan again around it
            if not self.replanned and cambc.fuel_remaining() > 1400000:
                self.replanned = True
                again = self.plan_fight(near, retreat)
                if again:
                    return again
            return act + (self.fallback_attack(near) or "move-failed")
        if ckind in (K_DAMAGE, K_AOE, K_CC):
            try:
                ok = cab.use(ctgt) if ckind == K_AOE else cab.use(near[ctgt][4])
                if ok:
                    self.can_cast = False
                    if cinfo.exclusive_with_movement:
                        self.can_move = False
                    act += "spell "
            except Error:
                pass
        elif ckind == K_HEAL:
            try:
                if cab.use(ctgt[7]):
                    self.can_cast = False
                    act += "heal "
            except Error:
                pass
        elif ckind == K_SHIELD:
            try:
                if cab.use():
                    self.can_cast = False
                    self.shield_until = self.rnd + cinfo.duration
                    act += "shield "
            except Error:
                pass
        if atk_i >= 0 and self.can_attack:
            try:
                me.attack(near[atk_i][4])
                self.can_attack = False
                if not self.melee:
                    self.can_move = False
                act += "hit "
                self.last_attack_round = self.rnd
            except Error:
                pass
        if ckind == 1 and atk2 >= 0 and self.can_cast:
            try:
                if cab.use():
                    self.can_cast = False
                    me.attack(near[atk2][4])
                    act += "reset+hit "
            except Error:
                pass
        if not act:
            act = "hold "
        return act + ("retreat" if retreat else "fight")
