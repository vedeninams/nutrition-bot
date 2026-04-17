# Wiki Instructions

This document tells you (the LLM) how to maintain each user's personal wiki — the compounding long-term memory the bot uses to know the user across days, weeks, and months.

## Inspiration

This memory architecture is inspired by Andrej Karpathy's essay ["LLM Wiki"](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

Key insight: rather than re-deriving knowledge from raw history on every query, the LLM builds and maintains a persistent, structured wiki. Knowledge compiled once, kept current — not re-discovered every time. Over weeks and months, the wiki becomes a synthesized picture of the user that makes the bot feel like a coach who truly knows them.

## Storage

Each user has their own wiki at `wiki/user_<id>/`, containing five markdown pages. The wiki is on disk so it's human-readable, version-controllable, and inspectable.

## The five pages

### profile.md — Who the user is
Durable personal facts that stay true over months. Dietary restrictions, allergies, medical notes, structural food preferences (e.g. vegetarian, lactose-intolerant), life context relevant to eating (e.g. shift worker, has a toddler, lots of travel). **No numeric targets here** — those belong in goals.md.

### goals.md — What they're working toward
Current calorie target, macro targets, weight target, current strategy, progress notes. All numbers and targets live here. Should reflect the current state, not every past goal.

### patterns.md — How they actually eat
Observed eating behaviors and recurring themes. Each bullet includes observation metadata: date first noticed, and observation count if seen multiple times.

Examples:
- Under-eats protein at breakfast (observed 5x since 2026-04-01)
- Prefers cold breakfasts (observed: 2026-04-17)
- Highest calorie density in evening meals (consistent Mar-Apr 2026)

### wins.md — What's worth celebrating
Real achievements, consistency streaks, milestones. Dated. This is what lets the bot give genuine, specific praise — not generic "great job" — because it remembers what was actually hard and what was overcome.

### log.md — What happened when
Chronological, append-only record of notable wiki events. Coarse-grained — 1-3 entries per day at most, not one per async ingest. Used for timeline reasoning ("when did I first start noticing X?"). Entries are prefixed with `## [YYYY-MM-DD]` for easy parsing.

## Ingest rules (when updating the wiki)

1. **Record observations, even single ones.** Don't filter aggressively at ingest — lint consolidates later. Single observations are noted with their date so the bot knows they're tentative.
2. **Flag contradictions in log.md.** If new information contradicts existing page content, note it in log.md and update the affected page.
3. **Keep pages concise.** Each page caps at ~30 bullet points. If a page approaches the cap, consolidate related bullets rather than dropping them.
4. **Only log meaningful events to log.md.** Not every ingest deserves a log entry. Log when: a new pattern is first observed, a contradiction is flagged, a milestone is hit, or weekly lint runs.
5. **Write nothing to profile.md unless it's durable.** If in doubt, put it in patterns.md as an observation instead. Promote to profile.md only after it's clearly stable.

## Query rules (when reading the wiki to answer the user)

1. **Read relevant wiki pages first.** The synthesis is already done there — no need to re-derive from raw data.
2. **Use natural language when referencing the wiki to the user.** Say "from what I've noticed about you..." or "looking at your patterns..." — NEVER cite file names like "per patterns.md" to the user. Internal reasoning can reference pages; user-facing output must not.
3. **Fall back to raw data** only when the wiki is silent on the question being asked.

## Lint rules (weekly)

1. **Consolidate redundancy.** Merge bullets that say the same thing in different words.
2. **Do NOT drop patterns speculatively.** Only remove a pattern if new data actively contradicts it. Silent absence — the pattern simply isn't seen recently — does NOT justify removal. Always check raw data before dropping anything.
3. **Check for contradictions across pages.** Flag any you find in log.md, then resolve them by updating the page with the more current info.
4. **Review goals.md** against recent progress. Update progress notes with current reality.
5. **Append one log.md entry** summarizing what lint did.
