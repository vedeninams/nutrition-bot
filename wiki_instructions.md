# Wiki Instructions

This document tells you (the LLM) how to maintain each user's personal wiki — the compounding long-term memory the bot uses to know the user across days, weeks, and months.

## Inspiration

This memory architecture is inspired by Andrej Karpathy's essay ["LLM Wiki"](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

Key insight: rather than re-deriving knowledge from raw history on every query, the LLM builds and maintains a persistent, structured wiki. Knowledge compiled once, kept current — not re-discovered every time. Over weeks and months, the wiki becomes a synthesized picture of the user that makes the bot feel like a coach who truly knows them.

## Storage

Each user has their own wiki at `wiki/user_<id>/`, containing five markdown pages. The wiki is on disk so it's human-readable, version-controllable, and inspectable.

## The five pages

### profile.md — Who the user is
Durable identity facts that are almost never changing. Dietary restrictions ("vegetarian"), allergies ("allergic to nuts"), medical notes, structural lifestyle facts (shift worker, has a toddler, lots of travel). Things you'd put on a medical intake form.

**DO NOT put active intentions here.** If the user is trying to do something — even something as simple as "eat less sweets" — that belongs in goals.md, not profile.md.

### goals.md — What they're working toward
Anything the user is actively trying to do, numeric or not. Examples:
  - "- **Daily calorie goal**: 1800 kcal"
  - "trying to lose 5 kg"
  - "cutting sugar for the next month"
  - "wants to eat less sweets"
  - "add more protein at breakfast"

Current strategies, progress notes, and target numbers all live here. Should reflect the current state, not every past goal.

**Canonical format for the daily calorie goal.** There is exactly ONE line in
goals.md that carries the numeric daily kcal target, and it MUST use this shape
so the bot can read and update it programmatically:

```
- **Daily calorie goal**: 1800 kcal
```

Rules for this specific line:
- Use the bullet prefix `- **Daily calorie goal**: N kcal` — bold label, colon,
  integer (commas allowed), the literal word `kcal`.
- If the user mentions a new calorie target, REPLACE the existing line rather
  than adding a second one. Only one canonical calorie-goal line should exist
  at any time. (If you see two, that's a bug and `/lint` will fix it.)
- Other goal bullets (kg targets, sugar, protein, habits) stay in their own
  free-form style — the canonical format applies only to the daily kcal target.

**Quick rule**: ask yourself "is this who they ARE (→ profile) or what they're trying to DO (→ goals)?". "I'm vegetarian" = ARE. "I'm trying to eat less meat" = DO.

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

0. **Stamp every line with `[YYYY-MM-DD]` at ingest.** In `profile.md`, `goals.md`, `patterns.md`, and `wins.md`, every new or edited bullet must start with today's date in square brackets, e.g. `- [2026-04-18] Vegetarian since 2019`. This internal metadata lets lint reason about recency (supersede the older of two contradicting lines, drop time-bounded goals past their window). Rules:
   - **On add:** prefix with today's date.
   - **On edit:** bump to today's date (the line reflects current knowledge).
   - **On merge:** keep the later of the two dates.
   - **Do NOT retroactively stamp** pre-convention lines that arrived without a prefix; leave them unprefixed. Lint treats unprefixed lines as pre-convention and won't try to guess their age.
   - **Do NOT stamp `log.md`** — it already uses `## [YYYY-MM-DD]` section headers.
   - The prefix is internal: it is stripped from `/profile` and any Telegram output. The user never sees it.
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
