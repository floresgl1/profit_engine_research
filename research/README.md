# Research

One-off analyses that inform the engine. Each script writes a generated
report next to it; this file holds the interpretation.

## Day window for KXHIGHNY settlement (`day_window.py`)

**Question.** Kalshi's NYC high-temperature markets settle on The Weather
Company's (TWC) number since 2026-08-14. Does TWC count the day in local
standard time (01:00 to 01:00 EDT in summer, like the NWS climate report)
or in local clock time (midnight to midnight)?

**Method** (plan agreed in review, refined after the first run):
measure how far observations miss the official high on days where the
window can't matter; keep only daylight-saving dates where one of the two
disputed midnight hours beats every other observation by more than that
error band, so the two windows predict non-overlapping highs; then check
which prediction the settlement value matches. The method runs on ACIS
(NWS, known to use LST) first as a check. Days with observation gaps over
90 minutes are skipped and isolated spikes (5°F+ above both neighbours)
are dropped; both problems showed up in the first run.

**Result** (`day_window.md`, data 2021-08-01 to 2026-10-01):

1. **Inconclusive.** No date in five years qualifies, not even for the ACIS
   check. Observations miss the official high by -0.8 to +3.0°F on
   daylight-saving days (-2.2 to +5.0°F all year), so a midnight hour would
   need to beat the rest of the day by 3.8°F or more; the largest such gap
   seen was 3°F, on 2 days. Public hourly data can't answer this question.
2. **It rarely matters.** The two windows' maximum observations differ on
   3.3% of daylight-saving days, by 2°F or more on 1.1%.
3. **TWC has matched NWS every time so far:** 49 of 49 TWC-era days settled
   on exactly the ACIS high. Small sample, summer and early autumn only.
4. **Kalshi and ACIS disagree occasionally in the NWS era:** 6 of 1,555
   days, including 2025-06-02 (Kalshi 66, ACIS 71). Likely causes are ACIS
   revisions after settlement or preliminary reports, which the contract
   says don't count. Not investigated further.
5. All settlement values are whole degrees.

**Implications for the temperature model.**

- **Training target:** Kalshi's `expiration_value` where it exists (it is
  literally what settled), the ACIS high otherwise. They agree on 99.6% of
  days.
- **Day window:** treat as unknown. Abstain, or widen uncertainty, on days
  when the expected high falls in a midnight hour (00:00 to 01:00 EDT at
  either end of the day). Expected cost: about 3% of daylight-saving days.
- **Revisit:** rerun as TWC days accumulate, especially in spring and
  autumn when fronts make midnight highs more likely. A single qualifying
  date decides it. Asking Kalshi support directly is the cheaper route.

Rerun: `uv run python research/day_window.py --out research/day_window.md`
