# Writing your own bot

A practical guide to understanding this game well enough to build a competitive Python bot yourself.
It assumes you are comfortable with Python and new to Battlecode.

Read sections 1–4 once, then work through the roadmap in section 7, benchmarking after every step.
The full references are [GameRules.md](GameRules.md) (rules and champion kits) and
[BotAPIPython.md](BotAPIPython.md) (every function); this guide tells you which parts matter.

**Contents**
1. [How a match works](#1-how-a-match-works)
2. [How your program runs](#2-how-your-program-runs)
3. [Your toolbox](#3-your-toolbox)
4. [The API you will actually use](#4-the-api-you-will-actually-use)
5. [A tour of the starter bot](#5-a-tour-of-the-starter-bot)
6. [What separates good bots from bad ones](#6-what-separates-good-bots-from-bad-ones)
7. [Roadmap: build your bot step by step](#7-roadmap-build-your-bot-step-by-step)
8. [Testing and submitting](#8-testing-and-submitting)

**Workspace layout**

| Path | What it is |
|---|---|
| `bots/my-bot/` | **Your bot.** Starts as a copy of the starter. |
| `bots/starter-python/` | The official starter, kept unchanged as an easy opponent. |
| `bots/v19/` | A stronger bot (beats the starter 79/80) to test against later. |
| `scripts/bench.py` | Plays two bots against each other on many maps and summarises the results. |
| `maps/` | The 40 maps in the current competition pool. |
| `docs/` | This guide, the game rules and the API reference. |
| `sdk/python/` | The SDK source. Your editor reads it for autocomplete; cambc manages it. |

Setup in a fresh environment is one command: `python3 -m pip install --upgrade cambc`.

---

## 1. How a match works

**Goal:** be the first team to *tribute* 1,000 gems at altars, or have more tributed after round 2,000
(ties go to lifetime gold earned, then kills, then team B).

**The economy loop** is the heart of the game:

1. **Kill a camp group.** The map has groups of monsters ("camps"). A group is all the camps
   that touch each other horizontally or vertically. Monsters sleep until hit, then attack the nearest champion every turn.
2. **Capture it.** When every monster in the group is dead, stand on one of its tiles for
   5 rounds with no enemy present. The group's gems and gold go to the allies standing there (split evenly).
3. **Carry the gems to an altar** and *tribute* them: stand next to the altar and channel for
   `ceil(gems / 10)` turns. Only tributed gems count. Carried gems are lost if you die.
4. **Spend gold** at your spawn on shards that permanently raise strength, health or focus.

Camps respawn after a delay (40–150 rounds), so the map is farmed over and over.

| Monster | Health | Damage/turn | Range | Gold | Gems | Respawn |
|---|---|---|---|---|---|---|
| Wolf | 1,350 | 20 | 3 | 40 | 4 | 40 |
| Elk | 2,400 | 60 | 4 | 90 | 15 | 60 |
| Golem | 5,000 | 120 | 4 | 160 | 36 | 80 |
| Gem cluster | 120 | 0 | – | 0 | 7 | 50 |
| Gold cluster | 120 | 0 | – | 20 | 0 | 50 |
| Monolith | 20,000 | 200 | 5 | 300 | 200 | 150 |

Clusters never fight back: a group of two elk plus three gem clusters pays 51 gems for 5,160 health of work.

**Death** costs a lot: you respawn after 60 rounds, and your carried gems go to whoever killed you
(plus 100 gold for them). A camp kill destroys the gems. Not dying is most of the game.

**The draft.** Before play, both teams pick 3–5 champions (depends on the map), one at a time,
simultaneously, and you see the enemy's picks as they happen. Both teams may pick the same champion. Each
champion has different stats and two abilities: see the roster in [GameRules.md](GameRules.md#champion-roster).

**A champion's turn:** one move (one tile, eight directions), one basic attack, and one ability cast.
Melee champions (range 1) can move and attack in the same turn. Ranged champions (range 3) must choose one.
All distances are *Chebyshev* (diagonal steps count as 1). Ability cooldowns are 8 turns.

**Vision:** each champion sees 8 tiles around itself. Walls and bushes block sight. **Teammates do
not share vision**: each of your champions only knows what it sees (plus its own ward).

**Other actions:** *recall* (5-turn channel back to spawn, cancelled by any damage), *wards*
(extra vision you can drop within 2 tiles, every 10 turns), *altar locks* (10 gold; block enemy
tributes at an altar until hit 5 times).

## 2. How your program runs

### One copy per champion, plus a drafter

During the draft, one instance of your program picks champions. When play starts, **every champion gets
its own instance of your program**. Each instance controls only its own champion and keeps its own
Python state between turns (objects, globals, caches), but instances cannot see each other's variables.

```python
# main.py: the loop every bot uses
def main():
    bot = Bot()                      # state that survives from turn to turn
    while True:
        try:
            if cambc.is_draft():
                draft.play()
            else:
                bot.play_turn()
        except Exception as error:   # never let one bad turn kill the bot
            print(f"bot error: {error}", flush=True)
        cambc.end_turn()             # hand control back; you resume here next turn
```

### Fuel: your compute budget

Each turn has a fixed budget of "fuel" (roughly, instructions executed). A Python bot gets about
**2.5 million per turn** (10x on the first draft turn, 2x on your first game turn). If you run out, your
program is **paused mid-function and resumes next round**, so the rest of that turn's actions slip a round.
Check what's left with `cambc.fuel_remaining()`. Practical rules:

- Compute static things once (distance fields to your spawn and altars, the list of camp groups) and cache them.
- Use the SDK's `Bitboard` and `DistanceField`: they run natively and cost almost no fuel.
  A pure-Python breadth-first search over a 100×100 map can easily blow the budget.

### Communication

- **Shared memory:** 8 bytes, readable and writable by all your champions all game.
  `cambc.shared_memory[i]` reads/writes one byte (0–255).
- **Draft memory:** 1,024 bytes written by the drafter (`cambc.draft_memory.submit(b"...")`), then
  read-only for your champions (`bytes(cambc.DraftMemoryBuffer())`). Good for telling each champion its role.

### Gotchas

- **No randomness:** `random` produces the same numbers every match. The starter seeds its own generator
  from the spawn position (`randomness.py`) if you want variety.
- **Engine calls only inside functions.** Your modules are imported before the match exists; a top-level
  `ME = cambc.me()` makes the bot silently do nothing. Call the API from `play_turn` (or functions it calls).
- **Standard library only.** No numpy or other packages.
- **Exceptions:** illegal actions raise `cambc.Error`. Movement onto a tile hidden in a bush returns
  `False` instead (the tile is then revealed), so check return values.
- **Don't hard-code game numbers.** Balance patches change stats every week. Read them live:
  `camp.base_stats()`, `me.strength()`, `ability.info().modifier`. (v19's biggest bug is a stale table.)

## 3. Your toolbox

Run everything from the workspace root.

| Command | What it does |
|---|---|
| `cambc run my-bot starter-python` | Play one match (first map alphabetically). First bot is team A (blue). |
| `cambc run my-bot v19 --map 00ambush --view` | Choose a map; open the replay afterwards. |
| `cambc run my-bot v19 --rounds 100` | A short smoke test (no result, but fast). |
| `cambc view replays/<file>.repl26a` | Open a saved replay in the visualiser (browser). |
| `python3 scripts/bench.py my-bot starter-python` | All 40 maps, both sides (~10–15 min). Add `--sample 10` for a quick check. |
| `cambc new other-bot --lang python` | Start another bot from the template. |
| `cambc bundle my-bot -o submissions` | Make a ZIP you can upload on the Submissions page. |
| `cambc login` then `cambc submit my-bot` | Submit straight from the terminal (login opens your browser). |

**Debugging.** `print(..., flush=True)` output is saved in each replay. The visualiser shows it
alongside the timeline. You can also draw on the map:

```python
cambc.debug.draw_dot(target_tile, 0xFF0000)               # red dot, visible in the replay
cambc.debug.draw_line(me.position(), target_tile, 0x00FF00)
```

Drawings last until that champion's next turn. Printing costs fuel, so keep a `DEBUG = False`
switch for chatty logs.

## 4. The API you will actually use

All examples assume `import cambc` and `from cambc import game_map, Team, Vec2`. Every one of them has been
run inside a real match.

### Yourself

```python
me = cambc.me()                       # your champion (only during play, not the draft)
pos = me.position()                   # Vec2 with .x and .y
me.health(), me.max_health()
me.gem_count(), me.gold_count()
me.strength(), me.focus(), me.attack_range()
me.is_alive(), me.can_move(), me.can_attack(), me.can_cast()
me.is_recalling(), me.is_submitting_gems()
```

Dead champions still get turns (to shop), so check `me.is_alive()` early in `play_turn`.

### The map (static) vs vision (what this champion sees right now)

```python
game_map.width(), game_map.height()
game_map.walls(), game_map.holes(), game_map.bushes(), game_map.altars()   # Bitboards
game_map.spawn_region(Team.ALLY)                                          # your spawn tiles
game_map.camp_groups()                # every camp group on the map, even out of sight

cambc.vision.camps()                  # camps you can see, with live health
cambc.vision.champions(Team.ENEMY)    # enemies you can see
cambc.vision.camp_tiles(), cambc.vision.champion_tiles()   # Bitboards of occupied tiles
```

### Bitboards: sets of tiles

A `Bitboard` is a set of map tiles with fast set operations. Use them for "where can I stand",
"what's blocked", "where are the goals".

```python
tiles = cambc.Bitboard()
tiles.draw_chebyshev(me.position(), 2)        # all tiles within 2 steps
blocked = game_map.walls() | game_map.holes()  # union
free = tiles & ~blocked                        # intersection and complement
free.is_set(Vec2(10, 12)), free.count(), free.positions()
```

### Distance fields: pathfinding

A `DistanceField` is a breadth-first search computed natively: the walking distance from a set of
"root" tiles to every tile. Compute it from your *goals*, then step downhill from where you stand:

```python
def blocked_tiles():
    me = cambc.me()
    blocked = (game_map.walls() | game_map.holes() | game_map.altars()
               | game_map.spawn_region(Team.ENEMY)
               | cambc.vision.champion_tiles() | cambc.vision.camp_tiles())
    blocked.set(me.position(), False)          # our own tile is not an obstacle
    return blocked


def walk_towards(goals):
    """One step towards the nearest free tile in `goals`. Returns True if we moved."""
    me = cambc.me()
    if not me.can_move():
        return False
    blocked = blocked_tiles()
    goals = goals & ~blocked
    if goals.is_empty() or goals.is_set(me.position()):
        return False
    field = cambc.DistanceField()
    field.compute(blocked, goals, cambc.Bitboard())
    step = field.next_move_from(me.position())  # a direction like Vec2(1, 0), or None
    return step is not None and me.move(step)
```

The same trick answers "how far is X": compute one field from your spawn at the start of the game,
then `field.get(tile)` is the walking distance from spawn to any tile (65535 = unreachable).

### Camp groups

```python
for group in game_map.camp_groups():
    camps = group.camps()
    health = sum(c.base_stats().base_health for c in camps)      # live stats: never hard-code these
    damage = sum(c.base_stats().base_strength for c in camps)    # total damage per turn if all awake
    group.gem_value(), group.gold_value(), group.spawn_tiles()   # spawn_tiles(): where to stand to capture
    camps[0].spawn_position()                                    # a tile of the group

    try:                                                         # state() only works while you can see the group
        state = group.state()
        if isinstance(state, game_map.CampGroupRespawning):
            print("back in", state.timer, "rounds")
        elif isinstance(state, game_map.CampGroupRewardAvailable):
            print("cleared: stand on spawn_tiles() to capture")
    except cambc.Error:
        pass                                                     # not in vision
```

### Acting

```python
me.move(Vec2.NORTH)                      # or any Vec2 step; returns False if a hidden unit blocks it
me.move_towards(target_tile)             # one step along the SDK's shortest path to a tile
me.attack(camp_or_enemy)                 # basic attack; target must be visible and in range
me.recall()                              # 5-turn channel home; damage cancels it
me.submit_gems(altar_tile)               # tribute; must be within 1 tile of the altar
me.place_ward(tile)                      # tile from me.ward_placement_tiles()
if me.can_buy(cambc.ItemType.POWER_SHARD_OF_STRENGTH, 2):
    me.buy(cambc.ItemType.POWER_SHARD_OF_STRENGTH, 2)    # 30 gold each, on spawn or while dead
```

**Shop:** shards cost 30 gold. Five matching shards combine into an emblem with a bonus:
Monster (+10% damage to camps), Jungle (−30% damage from camps), Durability (−10% damage taken),
Treasure (+20% gold), Home (faster recall), Power (one extra shard's worth of stats), Bruiser (spread stats).
Item names follow the pattern `cambc.ItemType.JUNGLE_SHARD_OF_HEALTH`.

### Abilities

```python
for ability in me.abilities():           # always two slots
    info = ability.info()                # AbilityDamage, AbilityCc, AbilityAoeDamage, AbilityDash,
                                         # AbilityShield, AbilityHeal or AbilityAttackReset
    if ability.is_ready() and ability.is_damage() and target_in_range:
        ability.use(target)              # unit target; ground abilities take a Vec2; self-casts take nothing
```

`info.range`, `info.modifier` (damage = modifier × focus) and `info.exclusive_with_movement` tell you
what a spell does without hard-coding champion names. An attack reset (`is_attack_reset()`) used right
after a basic attack gives you a second hit that turn.

### The draft

```python
PRIORITY = [cambc.ChampionDraft.HEART_OF_RUIN, cambc.ChampionDraft.MARCELLA,
            cambc.ChampionDraft.VESPER_VALE, cambc.ChampionDraft.RYTH, cambc.ChampionDraft.VENDETTA]

def play():
    ours = cambc.draft.current_draft(Team.ALLY)       # picks so far, as ChampionDraft values
    theirs = cambc.draft.current_draft(Team.ENEMY)    # visible as they are made
    for pick in PRIORITY:
        if pick not in ours:
            cambc.draft.pick(pick)
            return
```

`cambc.draft.team_size()` says how many picks this map allows. During the draft you can read the whole map
(`game_map`) and any champion's design: `d = cambc.Champion.design(cambc.ChampionType.MARCELLA)`, then
`d.base_stats()` and `d.abilities()` (live calls such as `d.health()` raise an error on a design).

## 5. A tour of the starter bot

`bots/my-bot` is a copy of the starter. Read its four files; they're short.

- **`main.py`**: the loop shown in section 2.
- **`draft.py`**: picks a random champion. Note `range(11)`: it never picks Heart of Ruin (id 11),
  the most-picked champion in top play.
- **`randomness.py`**: a small seeded random number generator (see "No randomness" above).
- **`bot.py`**: each turn, in priority order:
  1. Write the round number to shared memory (a demo of the API).
  2. Skip the turn while channelling (recall, tribute, teleport).
  3. On spawn: spend all gold on shards of one randomly chosen stat.
  4. Below 35% health and away from spawn: recall.
  5. Attack the first enemy champion in range.
  6. Carrying *any* gems: walk to the nearest altar and tribute.
  7. Otherwise: attack a camp in range, or walk to the nearest camp spawn tile.

**Why it is weak** (each of these is a step in the roadmap):

- It walks into whatever camp is nearest, whatever its strength. In a starter-vs-starter game each team
  loses ~36 champions to camps. That's the single biggest problem.
- All champions chase the same nearest camps instead of splitting the map.
- It banks after every capture, even with 4 gems, wasting walking time.
- It ignores its abilities completely.
- Its draft and shopping are random.
- It recalls while camps are hitting it (damage cancels the recall).

## 6. What separates good bots from bad ones

Lessons from benchmarking v19 against the starter, and from the current top-of-ladder drafts.

**Economy first.** v19 wins 79/80 against the starter mostly by farming well and *not dying*: about
4 deaths per game versus the starter's 34, and 978 gems tributed versus 126. Good farming means:

- **Only fight camps you can beat.** Before committing, estimate time to kill (total health ÷ your damage
  per turn) and damage taken (their damage per turn × that time). If you can't survive it, skip it or
  bring a teammate. Read camp stats live. v19 still dies a lot on Golem maps because its stats table
  thinks a Golem is a Wolf.
- **Choose targets by value per turn:** `(gems + some value for gold) / (walk + kill + capture time)`.
  Skip groups you know are respawning.
- **Split the work.** Use shared memory so two champions don't walk to the same group.
- **Bank in batches.** Tributing takes `ceil(gems/10)` turns either way, so carrying 40 gems costs no
  more tribute time than 4. But carried gems are lost on death, so bank before entering danger and
  near the end of the game (round 2,000).
- **Recall smartly.** It takes 5 turns and any damage cancels it, so step out of camp range first.
- **Shop with intent.** Jungle (less camp damage) and Monster (more camp damage) emblems are farming
  multipliers; health keeps you alive.

**Fight only when favoured.** Killing an enemy steals their whole bag and takes them out for 60 rounds,
so fights matter, but a lost fight hands them your bag. Compare total health × damage of each side, focus
your damage on one target, and retreat when outnumbered. Enemies on their own spawn heal every turn: don't chase them there.

**Never let one rule freeze the bot.** v19 can score 0 gems in a game: when it can see an enemy that
won't approach, its fight logic "holds" every turn and never gets back to farming. Whatever your
priorities are, make sure there's always a fallback to useful work.

**Draft for the current patch.** In the top 20 on patch 1.0.2, Heart of Ruin is picked in 81% of games,
Marcella and Vesper Vale 77%, Ryth 73%, Vendetta 46%. The most common 5-pick team is all five. This changes
every patch, so check the ladder and patch notes.

## 7. Roadmap: build your bot step by step

Make one change at a time, benchmark it, and commit when it helps. Start against `starter-python`
(quick wins), then switch to `v19` once you beat the starter comfortably.

**Step 0: baseline.** Run `python3 scripts/bench.py my-bot starter-python --sample 10`. An unchanged
copy should win about half. Note the deaths-to-camps line: that's what you'll drive down first.

**Step 1: draft.** Replace the random pick with a priority list (section 4). Optional: give each champion
its role through draft memory. *Check:* the draft column in the benchmark output.

**Step 2: don't die to camps.** Before walking to a group, estimate whether this champion can kill it:
- your damage per turn ≈ `me.strength()` (plus spells); time to kill = group health ÷ that;
- damage you take ≈ group damage per turn × time to kill;
- skip the group if that's more than, say, 60% of your current health.

*Check:* deaths to camps should fall sharply, and wins should rise.

**Step 3: pick the best group, not the nearest.** Compute a distance field from your position (or cache
one from spawn) and score every safe group by `(gems + 0.2 × gold) / (distance + time to kill + 5)`.
Remember groups you saw respawning and skip them until they're back. *Check:* gems tributed per game.

**Step 4: split the map.** Give each champion a slot (its order in the draft works) and store "the group
I'm working on" in shared memory byte `slot`. Others skip groups already claimed. *Check:* gems per game,
and in a replay, champions spreading out.

**Step 5: bank in batches.** Carry gems until you have, say, 30+ (or more if the altar is far), then bank.
Always bank if an enemy is near, you're about to recall, or the game is about to end. *Check:* gems per game.

**Step 6: retreat and recall properly.** When low, step out of camp range before recalling. Don't
recall while being hit. *Check:* deaths to camps, again.

**Step 7: use abilities on camps.** Damage spells and stuns on the toughest camp in range, attack resets
right after a basic attack, shields when taking damage, Heart of Ruin's dash (it damages and stuns where
it lands). *Check:* game length (faster farming means earlier wins).

**Step 8: shopping.** A build per role: e.g. Jungle and Monster emblems early for farmers, then Power
shards. *Check:* gems per game and deaths.

**Step 9: champion fights.** Now benchmark against `v19`. Decide fights by comparing both sides'
health × damage, focus one target, chase enemies carrying gems, retreat from bad fights. *Check:* deaths
to enemies versus theirs.

**Step 10: denial.** Altar locks on enemy altars, stepping into enemy captures to reset their timer,
wards on contested routes.

Watch replays of your losses at every step: the benchmark lists games sorted worst first.

## 8. Testing and submitting

- **Benchmark both sides of every map.** Team B wins exact ties and maps aren't always fair; `bench.py`
  plays each map twice for this reason.
- **Use full matches** for decisions; `--rounds` is only for checking that nothing crashes.
- **Watch the fuel line** in the benchmark output: frequent run-outs mean your bot's actions are slipping
  a round. Python is expensive; cache more.
- **Test everything before submitting:** run the full 40-map benchmark against your previous version so a
  change that helps one map doesn't break three others.
- **Submitting:** `cambc login` (approve in the browser), then `cambc submit my-bot`, or
  `cambc bundle my-bot -o submissions` and upload the ZIP on the Submissions page. Follow compilation on
  that page. When a patch breaks the SDK, you get 24 hours to resubmit before you start losing ranked
  matches automatically.
- **Keep up with patches** (handbook → Patch notes). After `python3 -m pip install --upgrade cambc`, run
  `cambc init` in the workspace to refresh the SDK and maps.
