# TWB Improvement Proposals

Gap analysis of the TWB bot vs. top-tier Tribal Wars bots (UTWB, SNT Bot) and the
script ecosystem (twscripts.dev, NAFS, Loot Assistant Enhancer, Fodox utilities).

The recommendations are tuned for a **fast farming world with milliseconds, archers,
watchtower, coin nobles, and building destruction active** — i.e. a world where
ms-precision actions, defensive interception, and mass operations all matter.

---

## 1. Current capabilities (baseline)

What TWB does well today:

- File-template build queue with lookahead + auto-rebuild on conquer
  ([game/buildingmanager.py](game/buildingmanager.py))
- Troop recruitment with smith research gating
  ([game/troopmanager.py:287](game/troopmanager.py#L287))
- Farming with scout-first, per-target cache, low/high-profile retry windows
  ([game/attack.py:248-329](game/attack.py#L248-L329))
- Advanced scavenging that splits troops across all 4 groups by capacity
  ([game/troopmanager.py:351](game/troopmanager.py#L351))
- Snob recruit covering both packet and coin systems
  ([game/snobber.py:62](game/snobber.py#L62))
- Defence flag rotation + auto-evacuation of `snob`/`axe` to other villages
  ([game/defence_manager.py:112-131](game/defence_manager.py#L112-L131))
- In-tribe support send/receive on incoming
  ([game/defence_manager.py:55](game/defence_manager.py#L55))
- Quest auto-complete + reward claim
  ([game/village.py:621-666](game/village.py#L621-L666))
- Market auto-balance + premium-points trading
  ([game/resources.py:393](game/resources.py#L393))
- Reports parsing with losses-aware refarm decision
  ([game/reports.py:56](game/reports.py#L56))

---

## 2. Gap analysis — ranked by impact

### Tier 1 — competitive blockers

| # | Gap | Where in code | Why it hurts on this world |
|---|---|---|---|
| 1 | **No millisecond timing engine for attacks.** `Hunter` is a dead skeleton, never instantiated from `village.run()`. | [game/hunter.py](game/hunter.py) | Milliseconds active, ±50ms command jitter — cannot send noble trains, cannot snipe, cannot backtime. |
| 2 | **No incoming-attack interception.** Bot raises a flag and evacuates fragile units only. | [game/defence_manager.py:71-110](game/defence_manager.py#L71-L110) | Cannot dodge (send out + recall), cannot snipe a noble train, cannot recall support after threat passes. |
| 3 | **No fake-attack generator.** | — | Wartime tool every tribe demands; needed to spread enemy defence. |
| 4 | **Captcha handling is fully manual** — `input("Press any key...")`. | [core/request.py:78-84](core/request.py#L78-L84) | Bot stalls indefinitely on hCaptcha. UTWB ships hCaptcha auto-solve as headline feature. |
| 5 | **No watchtower integration.** Bot reads zero data from incoming list. | — | Cannot tag noble trains by interval pattern, cannot schedule defensive actions relative to landing. |
| 6 | **Per-target farm scaling is fixed.** Static `template`; no continuous adaptation to scouted resources. | [game/attack.py:97](game/attack.py#L97) | Loot Assistant's `C` template (scout-aware) is the standard. Leaves loot on the ground. |

### Tier 2 — significant lift in mid-late game

| # | Gap | Where in code | Why it hurts |
|---|---|---|---|
| 7 | **No mass operation planner.** | — | Cannot orchestrate "all 12 off villages → coord X at landing T+0..T+800ms". 60-day victory rush needs this within weeks. |
| 8 | **Coin minting is reactive, not strategic.** Mints 1 coin when needed. | [game/snobber.py:124](game/snobber.py#L124) | Pre-minting in deff villages saves cumulative noble cost (`1+2+...+n`); also coordinates which village hosts the next academy. |
| 9 | **Simulator is unused.** Stat tables exist but no caller. | [game/simulator.py](game/simulator.py) | Cannot pick off template based on scouted defence (ram-heavy if HC, axe-heavy if foot), cannot predict if defence will hold. |
| 10 | **No noble target selection.** Snobber creates noble; nothing decides where to send. | — | Should evaluate: distance < 100, barb/inactive priority, 1500-2500 points sweet spot, loyalty regen math. |
| 11 | **No building-destruction recovery.** Build queue does not re-add downgraded levels. | [game/buildingmanager.py](game/buildingmanager.py) | Building destruction is active in this world. Cats burn wall 20 → 12, queue moves on. |
| 12 | **No cross-village farm intel sharing.** Per-village cache, no merge. | [game/attack.py:399-414](game/attack.py#L399-L414) | Village A scouts barb X, village B re-scouts the same barb. Wasted spies. |
| 13 | **Builder funds gating is naive.** Stops recruiting if short on funds. | [game/village.py:374-397](game/village.py#L374-L397) | Could pick cheapest queueable item, or trade resources via market to unblock. |

### Tier 3 — quality-of-life / scaling

| # | Gap | Notes |
|---|---|---|
| 14 | **No multi-account / sitter support.** One `config.json` = one account. | Sittings allowed for 30/60 days in this world; tribe leadership often sits. |
| 15 | **Anti-detection is shallow.** Only random delays. | No human-like browse, no warm-up after offline gaps, no UA rotation, no occasional clicks on overview/map/ranking. |
| 16 | **No knight skill management.** | Knight is active with skill leveling; bot ignores. |
| 17 | **No nomad-village handling.** | World has nomadic settlements; bot doesn't know they exist. |
| 18 | **Web dashboard is read-only and poll-based.** | [webmanager/](webmanager/) reads cache files. No websocket, no live attack feed, no "send fake from this village" button. |
| 19 | **No statistics view.** | Cannot easily answer "how much did the bot loot last 24h?" or "how many farms are draining?" |
| 20 | **Quest auto-claim doesn't optimize order.** | [game/village.py:635](game/village.py#L635) claims first-eligible reward. Should sort by `reward / cost`. |

---

## 3. Recommended build order

Order is **pain-relief first, complexity-vs-payoff balanced**. Each item is
independent — pick any subset.

### Phase A — defence (~3-4 days)

1. **Captcha → 2captcha / anti-captcha API integration** *(0.5 day)*
   - Replace the `input()` blocker in [core/request.py:83](core/request.py#L83).
   - Send screenshot of the challenge to the API, post solution back.
   - Telegram pause/resume notification.

2. **Incoming-attack parser → dodge engine** *(2 days, biggest defensive win)*
   - New `game/incoming.py`: parse `screen=info_command`, extract `(arrival_ts, slowest_unit, has_noble)` per attack.
   - Extend `DefenceManager` with a `dodge_plan` method:
     1. T-15s before landing → send all troops to a scavenge run or short attack
     2. T-2s → cancel the run (troops return at landing+epsilon)
   - For noble trains: detect 100-300ms gap pattern, plan **support snipe**
     (defence arrives between clear and noble landings).

3. **Building-destruction watcher** *(0.5 day)*
   - After every overview parse, diff `levels` with last cycle.
   - On regression, prepend the missing level back into `builder.queue`.

### Phase B — offence (~3 days)

4. **Hunter wired into main loop** *(1 day)*
   - Cache file `cache/schedule.json` of `(send_at_ms, source_vid, target_vid, troops, type)`.
   - `twb.py` main loop checks `Hunter.nearing_schedule_window(active_delay)`; if so, switches to `priority_mode` and pre-opens the place page.
   - Send at exact ms via `time.sleep(0.001)` busy-wait (already partially done in [game/hunter.py:59-62](game/hunter.py#L59-L62)).

5. **Fake-attack generator** *(0.5 day)*
   - Helper `send_fake(target, source, arrival_ts)` — 1 spy + 1 cat (or single ram) with random ±200ms arrival jitter.
   - Webmanager button "send N fakes from group X to coords Y at T".

6. **Simulator-driven farm scaling** *(1 day)*
   - On scout report parse, store `target_resources` in the per-target cache.
   - Replace fixed farm `template` with `simulator.calc_required_haul(resources)` → builds the smallest troop set that fits the loot.
   - Preserves min-spy attached as in current behaviour.

### Phase C — economy & strategy (~2 days)

7. **Cross-village farm intel cache** *(0.5 day)*
   - Move `cache/attacks/{vid}.json` to `cache/farms/{target_vid}.json` keyed by **target**, with `last_attacker` field.
   - Any village querying a target gets the latest scout/attack data, not just its own.

8. **Coin pre-minting strategy** *(0.5 day)*
   - New per-village config: `mint_coin_reserve: N`.
   - When village has spare resources and own academy, mint up to N coins regardless of snob demand.

9. **Noble target selector** *(1 day)*
   - When a noble is ready, scan `Map.villages` for candidates within 100 fields,
     filter `points ∈ [1500, 2500]`, prefer `owner == "0"` (barb) or inactive owner,
     compute walking time + remaining loyalty regen, pick highest-EV target.
   - Auto-launch noble + clear via `Hunter`.

### Phase D — polish (~optional)

10. **Live web dashboard** *(2 days)* — websocket feed of incoming attacks, live troop counts, manual fake/attack buttons.
11. **Sitter / multi-account support** *(2 days)* — config-as-list, one wrapper per account, separate cookie jars.
12. **Anti-detection humanizer** *(1 day)* — random visits to `screen=ranking` / `screen=map` between actions, gaussian-distributed delays, occasional 5-15min idle gaps during "active" hours.
13. **Knight skill auto-leveler** *(0.5 day)* — pick farm-haul or attack-bonus skills based on village role (off vs deff).

---

## 4. Effort summary

| Phase | Items | Effort | Expected EV gain |
|---|---|---|---|
| A — Defence | Captcha, dodge engine, destruction recovery | ~3-4 days | Very high — saves villages on a 300-day world |
| B — Offence | Hunter loop, fakes, sim-driven farms | ~3 days | High — enables ops, +10-20% farm yield |
| C — Economy | Farm cache merge, coin pre-mint, noble picker | ~2 days | Medium — compounds over 60+ days |
| D — Polish | Dashboard, sitter, humanizer, knight | ~5 days | Low-medium per item but ban-risk reduction is real |

Total to ship Phases A+B+C: **~9 working days** for one developer.

---

## 5. World-specific notes (current world)

These items become **especially** valuable given current world settings:

- **Milliseconds active** → Hunter / dodge / snipe (Tier 1.1, 1.2)
- **Watchtower active** → incoming parser (Tier 1.5)
- **Building destruction active** → destruction recovery (Tier 2.11)
- **Coin nobles + 100-field max distance** → noble target selector (Tier 2.10)
- **5-day newbie protection only** → captcha resilience matters from day 1 (Tier 1.4)
- **Sittings limited to 30/60 days** → multi-account support pays off (Tier 3.14)
- **Tribe lock at 180d, resource sending OFF** → cross-village farm intel matters more than resource balancer (Tier 2.12 over Tier 3.x)
- **Nomad settlements active** → Tier 3.17 becomes relevant late game

Items that are **not** worth pursuing on this world:

- Resource sending balancer between own villages (sending OFF in this world)
- Stronghold/relic auto-management (relics yes but stronghold is off)

---

## 6. References

- [Ultimate Tribal Wars Bot — features](https://ultimatetribalwarsbot.net/)
- [SNT Bot — features](https://sntbot.org/desktop)
- [Sniping in Tribal Wars — VivekDragon](https://vivekdragon.wordpress.com/2012/12/09/sniping-in-tribal-wars/)
- [Backtiming in Tribal Wars — VivekDragon](https://vivekdragon.wordpress.com/2012/12/09/backtiming-in-tribal-wars/)
- [Mass Attack Planner — twscripts.dev](https://twscripts.dev/scripts/mass-attack-planner/)
- [Fodox fake script utility](http://www.fxutility.net/fake_eng.php)
- [Loot Assistant Enhancer (C-template farming)](https://github.com/ntoombs19/LA-Enhancer)
- [TWStock premium exchange script](https://github.com/WoofThatByte/TWstock-Tribal-Wars-premium-market-script)
- [stefan2200/TWB README + CHANGELOG](https://github.com/stefan2200/TWB)
