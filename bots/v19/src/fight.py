"""Camp combat: perception, retreat, abilities, fighting and capture.

Adapted from v18's farming bot (plus a few ball helpers it shares): champions scan
camps and enemies, back out of danger while they still can, kill camps in threat
order, and stand on the spawn tiles until the reward lands.
"""
import cambc
import tactics as X
from cambc import game_map
from world import (cheb, is_big, UNREACHED, CAP_TURNS, CAPTURE_TIMEOUT,
                   WAIT_NEAR_MAX, CHOOSE_MIN_FUEL, BIG_PARTY,
                   K_RESET, K_DAMAGE, K_AOE, K_CC, K_HEAL, K_SHIELD,
                   M_HOME, C_HEAD, C_FIGHT, C_DONE)

ALLY = cambc.Team.ALLY
ENEMY = cambc.Team.ENEMY
Vec2 = cambc.Vec2
Bitboard = cambc.Bitboard
Error = cambc.Error


class FightMixin:
    # ------------------------------------------------------------------ perception
    def scan(self):
        """Collect live camps in view, damage we are taking and nearby enemies."""
        px, py = self.px, self.py
        camps = []
        nearest = 99
        inc = 0.0
        eff = self.hp + self.shield_value()
        prev = self.prev_eff
        took = prev is not None and eff < prev - 0.5
        self.took_amt = prev - eff if took else 0.0
        rnd = self.rnd
        if not cambc.vision.camp_tiles().is_empty():
            groups = self.groups
            chp = self.camp_hp
            awk = self.camp_awake
            for c in cambc.vision.camps():
                p = c.position()
                x, y = p.x, p.y
                idx = self.tile_idx.get((x, y))
                if idx is None:
                    continue
                hp = c.health()
                gi, ci = idx
                camps.append((x, y, hp, gi, ci, c, p))
                d = cheb(px, py, x, y)
                if d < nearest:
                    nearest = d
                if d <= X.CAMP_REACH:
                    inc += groups[gi].st[ci]
                    if took:
                        awk[(x, y)] = rnd           # something in reach just hit us
                before = chp.get((x, y))
                if before is not None and hp < before - 0.5:
                    awk[(x, y)] = rnd               # somebody (us or a teammate) is hitting it
                chp[(x, y)] = hp
        self.camps = camps
        self.nearest_camp = nearest
        # Camps wake when damaged, keep attacking while a champion is in range, and doze off after
        # two rounds without one. Sleeping camps never attack.
        if took:
            self.took_round = self.rnd
        if took or self.rnd - self.last_attack_round <= 1:
            self.awake_round = self.rnd
        self.awake = self.rnd - self.awake_round <= 2
        # Camps hit the champion nearest to them, so a teammate may be soaking it all: count incoming
        # damage only while it is really landing on us.
        self.inc = inc * self.mult if (self.awake and self.rnd - self.took_round <= 1) else 0.0
        enemies = []
        if not cambc.vision.champion_tiles(ENEMY).is_empty():
            for e in cambc.vision.champions(ENEMY):
                try:
                    ep = e.position()
                    ehp = e.health()
                    est, emax, ect = 0, ehp, 0
                    try:
                        est = int(e.state())
                        emax = e.max_health()
                        ect = int(e.champion_name())
                    except Error:
                        pass
                    egem = 0
                    try:
                        egem = int(e.gem_count())
                    except Exception:
                        pass
                    enemies.append((ep.x, ep.y, ehp, e.strength(), e, ep, e.attack_range(), e.focus(), ect, emax, est, egem))
                except Error:
                    pass
        self.enemies = enemies
        if enemies:
            sg = self.sightings
            for e in enemies:
                sg.append((self.rnd, e[0], e[1]))
            if len(sg) > 12:
                del sg[:len(sg) - 12]
        # remember damage on the group we are working on
        g = self.task
        if g is not None and camps:
            seen = [c for c in camps if c[3] == g.i]
            if seen:
                left = list(g.hpleft) if g.hpleft is not None else list(g.hp)
                vis = cambc.vision.visible_tiles()
                seen_ci = set()
                for c in seen:
                    left[c[4]] = c[2]
                    seen_ci.add(c[4])
                for ci in range(g.n):
                    if ci not in seen_ci and vis.is_set(g.vec[ci]):
                        left[ci] = 0.0
                if g.hpleft != left:
                    g.hpleft = left
                    g.hpsum = sum(left)
                    g.hp_ver += 1

    def scan_allies(self):
        out = []
        px, py = self.px, self.py
        try:
            for a in cambc.vision.champions(ALLY):
                ap = a.position()
                if ap.x == px and ap.y == py:
                    continue
                if not a.is_alive():
                    continue
                st = 0
                try:
                    st = int(a.state())
                except Error:
                    pass
                out.append((ap.x, ap.y, a.health(), a.max_health(), a.strength(), a.focus(),
                            int(a.champion_name()), a, ap, st))
        except Error:
            pass
        self.allies = out

    def check_group_done(self):
        g = self.task
        if g is None or self.cstate != C_FIGHT or self.cap_since == 0:
            return
        if cheb(self.px, self.py, int(g.cx), int(g.cy)) > 8:
            return
        try:
            state = g.cg.state()
        except Error:
            return
        if isinstance(state, game_map.CampGroupRespawning):
            self.finish_group(g, state.timer)

    # ------------------------------------------------------------------ retreat
    def maybe_retreat(self):
        hp = self.hp + self.shield_value()
        inc = self.inc
        # enemies able to hit us add their damage as well
        enemy_need = 0.0
        for e in self.enemies:
            if cheb(self.px, self.py, e[0], e[1]) <= e[6] + 1:
                enemy_need += (e[3] + 0.2 * e[7]) * 2.0
        base = self.maxhp * 0.06 + enemy_need
        if inc <= 0.0 and base <= self.maxhp * 0.1:
            return None
        # Once we have broken off hurt we stay out until healed, even if the next tile looks safe enough.
        hold = self.mode == M_HOME and self.hp < self.maxhp * 0.6 and inc > 0.0
        if not hold:
            if hp > inc * 9.0 * self.margin + base:
                return None                   # healthy enough that the exact way out cannot matter yet
            steps = self.steps_to_safety()
            if steps is None:
                steps = 4 - self.nearest_camp if self.nearest_camp < 4 else 1
            need = inc * (steps + 1.0) * self.margin + base
            if hp > need:
                return None
        # nearly dead camps: finish them instead of running
        if self.camps and not hold:
            left = sum(c[2] for c in self.camps if cheb(self.px, self.py, c[0], c[1]) <= 3)
            if left <= self.dps * 2.0 and hp > inc * 3.0:
                return None
        act = self.cast_shield()
        if self.hp < self.maxhp * 0.6:
            self.mode = M_HOME
        if self.can_move:
            moved = self.step_to_safety()
            if moved:
                return act + "retreat"
        return None

    def plan_safety(self):
        """Fill df_path with the way out of every tile a camp (or enemy) can reach; returns the steps needed."""
        if self.safe_round == self.rnd:
            return self.safe_steps
        self.safe_round = self.rnd
        self.safe_steps = None
        danger = Bitboard()
        groups = self.groups
        for c in self.camps:
            if groups[c[3]].st[c[4]] > 0.0:        # clusters never hit back
                danger.draw_chebyshev(c[6], X.CAMP_REACH)
        for e in self.enemies:
            danger.draw_chebyshev(e[5], 2)
        safe = ~danger & ~self.dyn_block()
        if safe.is_empty():
            return None
        walls = self.dyn_block() & ~safe
        walls.set(self.pos, False)
        dest = Bitboard()
        dest.set(self.pos)
        self.df_path.compute_to(walls, safe, dest, self.scratch)
        d = self.df_path.get(self.pos)
        if d < UNREACHED:
            self.safe_steps = d
        return self.safe_steps

    def steps_to_safety(self):
        return self.plan_safety()

    def step_to_safety(self):
        """Move toward the nearest tile that no live camp can reach."""
        if self.plan_safety() is None:
            return False
        return self.follow(self.df_path)

    # ------------------------------------------------------------------ enemy sightings
    def sight_block(self):
        """Tiles near two or more recent enemy sightings: an ambush zone we walk around when alone."""
        rnd = self.rnd
        sg = self.sightings
        if len(sg) < 2:
            return None
        for a in self.allies:
            if cheb(self.px, self.py, a[0], a[1]) <= 6 and not (a[9] & (2 | 4 | 4096)):
                return None                      # we have company: the fight planner handles ambushes
        recent = [(t, x, y) for (t, x, y) in sg if rnd - t <= 30]
        if len(recent) < 2:
            return None
        out = None
        n = len(recent)
        for i in range(n):
            t1, x1, y1 = recent[i]
            for j in range(i + 1, n):
                t2, x2, y2 = recent[j]
                if cheb(x1, y1, x2, y2) <= 8:
                    d1 = Bitboard()
                    d1.draw_chebyshev(Vec2(x1, y1), 5)
                    d2 = Bitboard()
                    d2.draw_chebyshev(Vec2(x2, y2), 5)
                    z = d1 & d2
                    if out is None:
                        out = z
                    else:
                        out |= z
        return out

    # ------------------------------------------------------------------ healing
    def try_heal(self):
        if not self.can_cast or not self.allies:
            return ""
        for ab, kind, info in self.abil:
            if kind != K_HEAL or ab.cooldown() != 0:
                continue
            full = info.modifier * self.mfocus
            best = None
            best_v = 0.0
            for a in self.allies:
                if cheb(self.px, self.py, a[0], a[1]) > info.range:
                    continue
                missing = a[3] - a[2]
                amt = full if missing > full else missing
                frac = a[2] / a[3] if a[3] > 0 else 1.0
                if amt >= 0.5 * full or (frac < 0.5 and amt > 0.2 * full):
                    v = amt + (200.0 if frac < 0.4 else 0.0)
                    if v > best_v:
                        best_v = v
                        best = a
            if best is not None:
                try:
                    if ab.use(best[7]):
                        self.can_cast = False
                        return "heal "
                except Error:
                    pass
        return ""

    # ------------------------------------------------------------------ movement helpers
    def dyn_block(self):
        b = self.static_block | cambc.vision.camp_tiles() | cambc.vision.champion_tiles()
        return b

    def aspawn_free(self):
        return self.aspawn & ~self.dyn_block()

    def follow(self, df):
        """Take one step along a distance field, avoiding units we can see."""
        self.revealed = False
        if not self.can_move:
            return False
        try:
            moves = df.next_moves_from(self.pos)
        except Error:
            return False
        for d in moves:
            try:
                if self.me.move(d):
                    self.px += d.x
                    self.py += d.y
                    self.pos = Vec2(self.px, self.py)
                    if not self.melee:
                        self.can_attack = False
                    self.can_move = False
                    self.moved = True
                    return True
                self.revealed = True            # an unseen unit (in a bush) is there: now we know about it
            except Error:
                continue
        return False

    def step_to(self, goals, avoid=None):
        """Walk one step toward the nearest goal tile; goals must be free tiles."""
        if not self.can_move or goals.is_empty():
            return False
        for _ in range(3):
            block = self.dyn_block()
            if avoid is not None:
                block |= avoid
            block = block & ~goals
            block.set(self.pos, False)
            dest = Bitboard()
            dest.set(self.pos)
            self.df_path.compute_to(block, goals, dest, self.scratch)
            if self.df_path.get(self.pos) >= UNREACHED:
                return False
            if self.follow(self.df_path):
                return True
            if not self.revealed:
                return False
        return False

    # ------------------------------------------------------------------ work / fight camps
    def committed(self):
        """Busy with a camp fight or a capture: do not walk away from it."""
        if self.task is not None and (self.cstate == C_FIGHT or self.cap_since > 0):
            return True
        if self.task is not None and self.camps and self.awake:
            for c in self.camps:
                if c[3] == self.task.i:
                    return True
        return False

    def work(self):
        act = self.work_once()
        if self.task is None and act not in ("no-task", "defer-choose"):
            # the task just ended without using the turn: pick the next one right away
            act += " | " + self.work_once()
        if self.task is None and act == "no-task":
            return "idle"
        if self.task is None and act.endswith(" | no-task"):
            return act[:-len(" | no-task")] + "idle"
        return act

    def work_once(self):
        # choose a group when we have none
        if self.task is None:
            f_before = cambc.fuel_remaining()
            if f_before < CHOOSE_MIN_FUEL:
                return "defer-choose"
            g = self.choose_task()
            if g is None:
                return "no-task"
            self.task = g
            self.cstate = C_HEAD
            self.done_rank = -1
            self.t_start = self.rnd
            self.cap_since = 0
            self.log(f"TASK {g.i} rank={g.rank} gems={g.gems} gold={g.gold} kind={g.kind}")
        g = self.task
        # more teammates on it than the party allows: the highest slots step aside
        cl = self.claims.get(g.rank)
        limit = self.kparty
        if is_big(g) and self.nslots >= BIG_PARTY and limit < BIG_PARTY:
            limit = BIG_PARTY
        if cl is not None and len(cl) >= limit:
            lower = 0
            for sl, st in cl:
                if sl < self.slot:
                    lower += 1
            if lower >= limit:
                self.task = None
                return "task-lost"
        px, py = self.px, self.py
        mine = [c for c in self.camps if c[3] == g.i]
        if mine:
            self.cstate = C_FIGHT
            return self.fight(g, mine)
        d_anchor = cheb(px, py, int(g.cx), int(g.cy))
        if d_anchor <= 8:
            state = None
            try:
                state = g.cg.state()
            except Error:
                state = None
            if isinstance(state, game_map.CampGroupRewardAvailable):
                return self.capture(g)
            if isinstance(state, game_map.CampGroupRespawning):
                return self.on_respawning(g, state.timer)
        # walk toward the camps
        return self.approach(g)

    def on_respawning(self, g, timer):
        """The group we came for is down: our own capture ended, or it is a short wait away."""
        if self.cstate == C_FIGHT and self.cap_since > 0:
            return self.finish_group(g, timer)
        g.ready_at = self.rnd + timer
        g.hpleft = None
        g.hpsum = g.hp_total
        g.hp_ver += 1
        self.cstate = C_HEAD
        if timer > WAIT_NEAR_MAX:
            # too long to idle here: come back when it is nearly up
            g.block_until = self.rnd + timer - WAIT_NEAR_MAX // 2
            self.task = None
            self.cap_since = 0
            return "group-later"
        return self.approach(g, waiting=True)

    def finish_group(self, g, timer):
        if self.cstate == C_FIGHT and self.cap_since > 0:
            g.ready_at = self.rnd + g.resp
            self.done_rank = g.rank
            self.log(f"CAPTURED {g.i} gems={self.gems} gold={self.gold}")
        else:
            g.ready_at = self.rnd + timer
            g.block_until = 0
        g.hpleft = None
        g.hpsum = g.hp_total
        g.hp_ver += 1
        self.task = None
        self.cap_since = 0
        self.cstate = C_HEAD
        return "group-done"

    def capture(self, g):
        self.cstate = C_FIGHT
        if self.cap_since == 0:
            self.cap_since = self.rnd
        if (self.px, self.py) in g.xyset:
            self.cw_n = 0
            self.cap_tile_n = self.cap_tile_n + 1 if self.cap_last == self.rnd - 1 else 1
            self.cap_last = self.rnd
            if self.rnd - self.cap_since > CAPTURE_TIMEOUT:
                g.block_until = self.rnd + 50
                self.task = None
                self.cap_since = 0
                return "capture-timeout"
            return "capturing" + self.capture_fire(g)
        self.cap_tile_n = 0
        goals = g.bb & ~self.dyn_block()
        if goals.is_empty():
            # someone already stands on every spawn tile: they collect, we move on
            g.ready_at = self.rnd + CAP_TURNS + g.resp
            self.task = None
            self.cap_since = 0
            return "capture-yield"
        if self.step_to(goals):
            return "to-capture"
        return "capture-stuck"

    def capture_fire(self, g):
        """Standing on the capture tile anyway: hit a camp in range instead of idling."""
        if not self.can_attack or not self.camps:
            return ""
        best = None
        for c in self.camps:
            if c[3] == g.i or cheb(self.px, self.py, c[0], c[1]) > self.arange:
                continue
            st = self.groups[c[3]].stsum
            if st > 0 and (self.hp < 0.7 * self.maxhp or st > 60):
                continue                          # only wake fighting camps when healthy and they are weak
            key = (st, c[2])
            if best is None or key < best[0]:
                best = (key, c)
        if best is None:
            return ""
        try:
            self.me.attack(best[1][5])
        except Error:
            return ""
        self.can_attack = False
        self.last_attack_round = self.rnd
        if not self.melee:
            self.can_move = False
        return "+hit"

    def approach(self, g, waiting=False):
        # target tiles: next to (melee) or in range of (ranged) the group's camps
        goals = Bitboard()
        for v in g.vec:
            goals.draw_chebyshev(v, self.arange)
        if waiting:
            goals &= ~g.bb              # keep the spawn tiles free for the respawn
        goals &= ~self.dyn_block()
        if goals.is_empty():
            g.block_until = self.rnd + 40
            self.task = None
            return "approach-blocked"
        if waiting and goals.is_set(self.pos):
            self.same_pos = 0
            return "wait-respawn"
        if self.last_pos == (self.px, self.py):
            self.same_pos += 1
        else:
            self.same_pos = 0
        self.last_pos = (self.px, self.py)
        ph = self.pos_hist
        ph.append(self.last_pos)
        if len(ph) > 10:
            del ph[0]
            if len(set(ph)) <= 2:
                self.same_pos = 99               # shuffling between two tiles gets us nowhere
                del ph[:]
        if self.same_pos > 8:
            g.block_until = self.rnd + 60
            self.task = None
            self.same_pos = 0
            return "approach-stuck"
        if self.step_to(goals, self.sight_block()):
            return "approach"
        return "approach-fail"

    def fight(self, g, mine):
        me = self.me
        arange = self.arange
        act = ""

        def key(c):
            # kill the camp with the most damage per remaining hp first, then the weakest
            return (g.st[c[4]] / max(c[2], 1.0), -c[2])

        def reachable(c):
            return cheb(self.px, self.py, c[0], c[1]) <= arange

        in_range = [c for c in mine if reachable(c)]
        if not in_range and self.can_move:
            goals = Bitboard()
            for c in mine:
                goals.draw_chebyshev(c[6], arange)
            block = self.dyn_block()
            if not self.melee:
                inner = Bitboard()
                for c in mine:
                    inner.draw_chebyshev(c[6], arange - 1)
                ring = goals & ~inner & ~block
                if not ring.is_empty():
                    goals = ring
            goals &= ~block
            if self.step_to(goals):
                act += "step "
                in_range = [c for c in mine if reachable(c)]
        if in_range:
            tgt = max(in_range, key=key)
            act += self.cast_spells(mine, tgt)
            if self.can_attack:
                try:
                    me.attack(tgt[5])
                    self.last_attack_round = self.rnd
                    self.fight_gi = g.i
                    self.can_attack = False
                    if not self.melee:
                        self.can_move = False
                    act += "hit "
                    act += self.cast_reset(tgt)
                    if sum(g.st) > 0:
                        act += self.cast_shield()
                except Error as error:
                    act += f"hit-failed({error}) "
        else:
            act += self.cast_spells(mine, None)
            act += "closing"
        return act

    # ------------------------------------------------------------------ abilities
    def shield_value(self):
        if self.rnd > getattr(self, "shield_until", -1):
            return 0.0
        try:
            buffs = self.me.buffs()
            if buffs.has_shield():
                return float(buffs.get(cambc.BuffShield).value)
        except Exception:
            pass
        return 0.0

    def cast_shield(self):
        if not self.can_cast:
            return ""
        for ab, kind, info in self.abil:
            if kind == K_SHIELD and ab.cooldown() == 0:
                try:
                    if ab.use():
                        self.can_cast = False
                        self.shield_until = self.rnd + info.duration
                        return "shield "
                except Error:
                    pass
        return ""

    def cast_reset(self, tgt):
        """Attack-reset abilities grant a second swing this turn."""
        if not self.can_cast:
            return ""
        for ab, kind, info in self.abil:
            if kind == K_RESET and ab.cooldown() == 0:
                try:
                    if ab.use():
                        self.can_cast = False
                        self.me.attack(tgt[5])
                        return "reset+hit "
                except Error:
                    pass
        return ""

    def cast_spells(self, mine, tgt):
        """Damage / stun spells on the toughest camp in spell range."""
        if not self.can_cast:
            return ""
        px, py = self.px, self.py
        for ab, kind, info in self.abil:
            if kind not in (K_DAMAGE, K_CC, K_AOE) or info.modifier <= 0:
                continue
            if info.exclusive_with_movement and self.moved:
                continue
            if ab.cooldown() != 0:
                continue
            best = None
            for c in mine:
                if cheb(px, py, c[0], c[1]) <= info.range:
                    if best is None or c[2] > best[2]:
                        best = c
            if best is None:
                continue
            try:
                ok = ab.use(best[6]) if kind == K_AOE else ab.use(best[5])
            except Error:
                continue
            if ok:
                self.can_cast = False
                if info.exclusive_with_movement:
                    self.can_move = False
                self.last_attack_round = self.rnd
                self.fight_gi = best[3]
                return "spell "
        return ""
