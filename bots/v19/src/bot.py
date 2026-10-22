"""Champion decisions for the farming bot. main.py dispatches here after the draft.

Strategy in one paragraph: every champion is an independent farmer that also defends itself.
It scores every camp group by (gems + weighted gold) per turn of work, claims the best free one
in shared memory so teammates pick different ones, walks there (dropping a ward toward the next
task), kills it with focus fire and abilities, stands on the spawn tile until the reward lands,
and moves on. Enemy champions are judged by a local strength ratio: fight at a comfortable edge
(>= 1.2, or 0.7 to keep an ongoing one), flee below it, and hunt a rich isolated farmer with
company. Carried altar locks cancel enemy tributes and deny their side; stepping into a camp
capture region they are collecting resets their timer. When hurt the champion backs out of camp
range, recalls, shops and returns. Gems are carried in batches and banked when that is cheap or
when the team can win.
"""
import cambc
import tactics as X
from world import (WorldMixin, DEBUG, UNREACHED, M_IDLE, M_HOME, M_BANK,
                   C_HEAD, C_FIGHT, ROUND_LIMIT, ENDGAME_SLACK, BANK_LATCH, CAP_TURNS)
from fight import FightMixin, ALLY, ENEMY, Bitboard, Error, cheb
from pvp import PvpMixin, BANK_NEAR, BANK_NEAR_GEMS, RECALL_SIGHT_AGE


class Bot(WorldMixin, FightMixin, PvpMixin):
    def __init__(self):
        self.inited = False
        self.task = None
        self.mode = M_IDLE

    # ------------------------------------------------------------------ turn entry
    def play_turn(self):
        self.rnd = cambc.round_number()
        try:
            if not self.inited:
                self.init_world()
                self.pvp_init()
            self.turn()
        except Exception as error:
            import sys
            tb = sys.exc_info()[2]
            lines = []
            while tb is not None:
                lines.append(tb.tb_lineno)
                tb = tb.tb_next
            print(f"ERR R{self.rnd} S{getattr(self, 'slot', -1)} {error!r} lines={lines}", flush=True)

    def turn(self):
        me = self.me
        f0 = cambc.fuel_remaining()
        st = int(me.state())
        self.st = st
        if not (st & 1):
            self.dead_turn()
            return
        self.can_move = bool(st & 8)
        self.can_attack = bool(st & 16)
        self.can_cast = bool(st & 2048)
        self.moved = False
        self.was_alive = True
        if st & (2 | 4 | 4096):          # recalling, tributing or teleporting: let the channel run
            self.gems = me.gem_count()
            self.read_shared()
            self.write_shared()
            self.prev_eff = me.health() + self.shield_value()
            return
        pos = me.position()
        self.pos = pos
        self.px, self.py = pos.x, pos.y
        self.hp = me.health()
        self.gems = me.gem_count()
        self.gold = me.gold_count()
        if self.stats_dirty:
            self.refresh_stats()
        self.read_shared()
        self.scan()
        self.scan_allies()
        act = self.decide()
        self.write_shared()
        self.prev_eff = self.hp + self.shield_value()
        used = f0 - cambc.fuel_remaining()
        if used > self.fuel_max:
            self.fuel_max = used
        if DEBUG:
            extra = ""
            if self.camps and (self.inc > 0.0 or self.mode == M_HOME):
                extra = " camps=" + str([(c[0], c[1], int(c[2])) for c in self.camps]) + f" inc={self.inc:.0f} aw={int(self.awake)}"
            if self.enemies:
                extra += " en=" + str([(e[0], e[1], int(e[2]), e[8], e[11]) for e in self.enemies]) + f" al={len(self.allies)} ratio={getattr(self, 'last_ratio', 0):.2f} eng={int(self.engaged)}"
            self.log(f"({self.px},{self.py}) hp={int(self.hp)}/{int(self.maxhp)} gems={self.gems} gold={self.gold} "
                     f"mode={self.mode} task={self.task.i if self.task else -1}/{self.cstate} fuel={used}/{self.fuel_max} {act}{extra}")

    def dead_turn(self):
        if self.was_alive:
            print(f"R{self.rnd} S{self.slot} DEAD", flush=True)
            self.was_alive = False
            g = self.task
            if g is not None and X.DEATH_BLOCK > 0:
                g.fails += 1
                g.block_until = max(g.block_until, self.rnd + min(2000, X.DEATH_BLOCK * g.fails))
        self.task = None
        self.mode = M_IDLE
        self.done_rank = -1
        self.gems = 0
        self.prev_eff = None
        self.cap_since = 0
        self.cstate = C_HEAD
        self.bank_latch = -1
        self.engaged = False
        self.spawn_hold = False
        self.hold_sp = 0
        try:
            gold = self.me.gold_count()
            if gold >= 30:
                self.gold = gold
                self.buy_items()
        except Error:
            pass
        self.read_shared()
        self.write_shared(alive=False)

    # ------------------------------------------------------------------ decisions
    def decide(self):
        px, py = self.px, self.py
        on_spawn = (px, py) in self.spawn_xy
        self.check_group_done()
        self.update_push()
        act = self.maybe_place_ward()
        act += self.maybe_lock()
        self.sync_lock()
        self.spawn_hold = self.want_spawn_hold()
        # 0. a loaded bag heads for the altar even with enemies about
        # (full bags die with us otherwise; skirmish then runs in retreat posture)
        if self.corner_seat != "B" and self.mode != M_BANK and self.gems > 0 and self.want_bank():
            self.mode = M_BANK
        # 1. enemy champions: fight them with the planner, or break off
        if self.enemies:
            act2 = self.skirmish()
            if act2 and not (act2.startswith("hold") and self.committed()):
                return act + act2
        else:
            self.engaged = False
            self.hold_fights = 0
        # 2. take cover if camps are hurting us too much
        if self.inc > 0.0:
            act2 = self.maybe_retreat()
            if act2:
                return act + act2
        act += self.try_heal()              # a healer tops up whoever is hurt (costs no movement)
        # 3. spawn: shop, heal, clear enemy locks off our altars
        if on_spawn:
            self.buy_items()
            act += self.break_lock()
            if self.hp < self.maxhp * 0.97 and not self.enemies:
                self.mode = M_IDLE
                return act + "heal-at-spawn"
            if self.mode == M_HOME:
                self.mode = M_IDLE
        elif self.field_shop and self.gold >= 30:
            self.buy_items()
        if self.spawn_hold:
            return act + "spawn-hold"
        # 4. deny the enemy a camp capture they are collecting (cheap when safe)
        if self.enemies:
            act2 = self.deny_capture()
            if act2:
                return act + act2
        # 4b. idle by a locked altar: land a hit on the lock
        if self.can_attack and not self.enemies and self.hp >= self.maxhp * 0.7:
            act += self.break_lock()
        # 5. go home to heal / shop (not while they are about to win: the bag decides the game, and not
        #    while a capture we are standing on is about to pay out and nothing can stop it)
        stay = self.capture_hold()
        if stay and self.mode == M_HOME:
            self.mode = M_IDLE
        if not stay and (self.mode == M_HOME or (self.need_home() and not self.emergency_bank())):
            self.mode = M_HOME
            return act + self.go_home()
        # 6. bank gems
        if self.mode == M_BANK or self.want_bank():
            act2 = self.do_bank()
            if act2:
                return act + act2
        # 7. hunt an isolated rich enemy we can take with company
        act2 = self.gank_hunt()
        if act2:
            return act + act2
        # 8. work on a camp group
        return act + self.work()

    # ------------------------------------------------------------------ stay or recall?
    def threat(self, turns):
        """(damage, first hit turn) coming at us over the next `turns` turns if we stand still: camps hitting us,
        visible enemies, and one enemy seen a moment ago that has since left our view."""
        px, py = self.px, self.py
        dmg, first = X.incoming_over(px, py, self.enemies, turns, cheb, skip=self.at_home, camp_dps=self.inc)
        rnd = self.rnd
        # Awake camps within reach hit us even on turns when nothing landed yet (elk especially): any hit cancels a recall.
        awk = self.camp_awake
        for c in (self.camps if X.USE_CAMP_THREAT else ()):
            st = self.groups[c[3]].st[c[4]]
            if st > 0.0 and cheb(px, py, c[0], c[1]) <= X.CAMP_REACH and (self.awake or rnd - awk.get((c[0], c[1]), -99) <= 3):
                dmg += st * self.mult * turns
                first = 1
        best = None
        for (t, x, y) in self.sightings:
            age = rnd - t
            if age < 1 or age > RECALL_SIGHT_AGE or cheb(px, py, x, y) > 7:
                continue
            if any(cheb(x, y, e[0], e[1]) <= age + 1 for e in self.enemies):
                continue                        # that is an enemy we can still see
            if best is None or age < best[0]:
                best = (age, x, y)
        if best is not None:
            age, x, y = best
            h = X.hit_turn(max(0, cheb(px, py, x, y) - age), 3)
            if h <= turns:
                dmg += max(self.mstr, 30.0) * 1.2 * (turns - h + 1)
                if h < first:
                    first = h
        return dmg, first

    def capture_hold(self):
        """True while a capture we are on (or a few steps from) is about to pay out and we expect to live through the wait.
        Recalling would throw the reward away, and the recall channel takes as long as the capture does."""
        g = self.task
        if not X.USE_CAPTURE_HOLD or g is None or self.cstate != C_FIGHT or self.cap_since == 0:
            return False
        if g.gems + g.gold <= 0:
            return False
        on = (self.px, self.py) in g.xyset
        if on:
            left = max(1, CAP_TURNS - self.cap_tile_n)
        else:
            dist = cheb(self.px, self.py, int(g.cx), int(g.cy))
            if dist > 8:
                return False
            left = CAP_TURNS + dist
        dmg, _ = self.threat(left + 1)
        eff = self.hp + self.shield_value()
        return eff > dmg * 1.25 + self.maxhp * 0.08

    def recall_safe(self, recall_t):
        """Nothing can hurt us before the recall lands (any damage cancels it)."""
        dmg, first = self.threat(recall_t)
        return dmg <= 0.0

    def need_home(self):
        if self.task is not None and self.camps and self.awake and self.nearest_camp <= 3:
            return False
        if self.hp < self.maxhp * 0.5:
            return True
        if self.gold >= self.shop_gold() and not self.field_shop:
            d = self.df_spawn.get(self.pos)
            if d <= 10 and self.task is None:
                return True
            if self.gold >= 400 and self.task is None:
                return True
        return False

    def shop_gold(self):
        return 150

    # ------------------------------------------------------------------ home / shop / bank
    def hot_zone(self):
        """Tiles within reach of camps that are awake, i.e. hit lately by us or a teammate (None when there are none)."""
        if not self.camps:
            return None
        rnd = self.rnd
        awk = self.camp_awake
        groups = self.groups
        zone = None
        for c in self.camps:
            if rnd - awk.get((c[0], c[1]), -99) <= 2 and groups[c[3]].st[c[4]] > 0.0:
                if zone is None:
                    zone = Bitboard()
                zone.draw_chebyshev(c[6], X.CAMP_REACH)
        return zone

    def walk_home(self):
        """One step toward the spawn, going around camps that are awake."""
        if not self.can_move:
            return False
        zone = self.hot_zone()
        if zone is not None:
            if self.step_to(self.aspawn_free(), zone):
                return True
            if not zone.is_set(self.pos):
                return False                  # the only way home crosses an awake camp: wait for it to doze off
        return self.follow(self.df_spawn)

    def go_home(self):
        d = self.df_spawn.get(self.pos)
        if d >= UNREACHED:
            return "home-unreachable"
        if (self.px, self.py) in self.spawn_xy:
            return "home-wait"
        recall_t = 3 if "HOME" in self.flags else 5
        # Only start the channel when nothing can hit us before it lands; otherwise keep moving home, and
        # when hurt and caught by someone faster, dash away.
        hunted = X.USE_RECALL_SAFE and not self.recall_safe(recall_t)
        if not X.USE_RECALL_SAFE:
            hunted = any(cheb(self.px, self.py, e[0], e[1]) <= e[6] + 5 for e in self.enemies)
        if hunted and self.dash_home():
            return "dash-home"
        if d <= recall_t + 1 or hunted:
            if self.walk_home():
                return "walk-home"
        # bank on the way when an altar is close to our path home
        if self.gems >= 20 and self.df_altar.get(self.pos) <= 8 and not hunted and self.hp >= self.maxhp * 0.5:
            act = self.do_bank()
            if act:
                return act
        if self.inc > 0.0:
            if self.step_to_safety():
                return "home-evade"
            if self.task is not None and any(c[3] == self.task.i for c in self.camps):
                return "home-cornered " + self.work()
            return "home-wait-danger"
        try:
            self.me.recall()
            self.recall_started = self.rnd
            return "recall"
        except Error as error:
            return f"recall-failed {error}"

    def buy_items(self):
        me = self.me
        gold = self.gold
        bought = False
        while gold >= 30 and self.set_pos < len(self.build):
            fam, stat = self.build[self.set_pos]
            it = self.item(fam, stat)
            cost = 30
            n = min(gold // cost, 5 - self.set_cnt)
            if n <= 0:
                break
            try:
                me.buy(it, n)
            except Error as error:
                self.log(f"buy-failed {error}")
                break
            gold -= cost * n
            self.set_cnt += n
            bought = True
            if self.set_cnt >= 5:
                self.set_cnt = 0
                self.set_pos += 1
                self.flags.add(("MON", "POW", "DUR", "TRE", "JUN", "HOME", "BRU")[fam])
        if bought:
            self.gold = gold
            self.stats_dirty = True
            self.log(f"bought sets={self.set_pos}+{self.set_cnt} gold_left={gold}")
        if gold >= 40 and not self.has_lock and self.lock_type is not None:
            try:
                me.buy(self.lock_type)
                self.has_lock = True
                gold -= 10
                self.gold = gold
                self.log("bought lock")
            except Error as error:
                self.log(f"lock-buy-failed {error}")
        return bought

    def bank_threshold(self, d):
        t = X.BANK_A * d + X.BANK_B
        if t < X.BANK_MIN:
            t = X.BANK_MIN
        if t > X.BANK_MAX:
            t = X.BANK_MAX
        return t

    def emergency_bank(self):
        """They are a bag or two from 1000: every gem we still carry is worthless once they cross."""
        return self.gems >= 10 and cambc.global_submitted_gems(ENEMY) >= 880

    def want_bank(self):
        gems = self.gems
        if gems <= 0:
            self.bank_latch = -1
            return False
        d = self.df_altar.get(self.pos)
        if d >= UNREACHED:
            return False
        # convert everything under threat too: carried gems are worth nothing if they win
        if self.emergency_bank():
            self.bank_latch = self.rnd + BANK_LATCH
            return True
        thr = self.bank_threshold(d)
        if self.corner_seat == "A":
            big = thr
        elif self.corner_seat == "B":
            big = 10 ** 9
        else:
            big = max(2 * thr, 150)             # only a very full bag braves enemies on the way
        if self.task is not None and self.camps and (self.inc > 0.0) and gems < big:
            return False
        if self.endgame_bank(d):
            self.bank_latch = self.rnd + BANK_LATCH
            return True
        if cambc.global_submitted_gems(ALLY) + max(self.tot, gems) >= 998:
            self.bank_latch = self.rnd + BANK_LATCH    # one more push and the team wins: put these gems in
            return True
        # a very full bag goes in even under threat: carried gems are worth nothing if we die with them
        if self.threat_near() and gems < big and not (d <= BANK_NEAR and gems >= BANK_NEAR_GEMS):
            return False                     # enemies about: only bank when the altar is right here
        if self.committed() and gems < big:
            return False
        if d <= 2 and gems >= 10:
            return True                      # standing on the altar anyway: dump the bag
        if self.rnd <= self.bank_latch or gems >= thr:
            self.bank_latch = self.rnd + BANK_LATCH
            return True
        return False

    def endgame_bank(self, d):
        """Close to the round limit carried gems are worth nothing: take them to an altar while there is time."""
        return ROUND_LIMIT - self.rnd <= d + (self.gems + 9) // 10 + ENDGAME_SLACK

    def do_bank(self):
        px, py = self.px, self.py
        for k, (ax, ay) in enumerate(self.altar_xy):
            if cheb(px, py, ax, ay) <= cambc.constants.SUBMIT_GEMS_RANGE:
                try:
                    self.me.submit_gems(self.altar_vecs[k])
                    print(f"R{self.rnd} S{self.slot} BANK {self.gems}", flush=True)
                    self.mode = M_IDLE
                    self.task = None
                    self.bank_latch = -1
                    return "submit"
                except Error as error:
                    return self.break_lock() + f"submit-failed({error})"
        self.mode = M_BANK
        if self.follow(self.df_altar):
            return "walk-bank"
        if self.step_to(self.altar_goals):
            return "walk-bank2"
        self.mode = M_IDLE
        return None
