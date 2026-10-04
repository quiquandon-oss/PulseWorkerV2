# CryptoPulse Research Lab — how Olivier uses it (with ChatGPT)

Staging: https://pulseworker-v2-staging.quiquandon.workers.dev/research-lab

Research Lab does the bookkeeping; **ChatGPT (or Claude, Gemini, Grok) does the research reasoning with you**.
Research Lab never calls an AI by itself and never changes production V1.

## The loop

1. **Market**: open it and pick an interesting event.
2. Read **What happened** and **Why? What V1's current sources say** (✓ explains, ? partly, ✕ pointed the other way).
3. If the sources do not explain it, press **Investigate why** to go to **Research**.
4. Press **COPY RESEARCH PACK**.
5. Paste it into ChatGPT. The pack already starts with this instruction:
   > Analyse this CryptoPulse research case. Identify the most likely missing market explanation and determine whether
   > CryptoPulse is missing a source, trend, signal or regime-specific factor. Separate evidence from speculation and
   > propose concrete sources/signals that could be added to V1.
6. Review ChatGPT's answer yourself: open the URLs, and make sure EVIDENCE, INFERENCE and SPECULATION are kept apart.
   If the JSON block at the end is missing, ask: *"Please end with the JSON block exactly as requested."*
7. Paste the whole answer into **PASTE AI RESULT** and structure it.
8. Check every field (finding, driver, new source, signal, trend, evidence, alternatives, confidence, URLs), tick the
   review box, and press **CONFIRM FINDING**. Nothing is accepted automatically.
9. Press **Create learning candidate**.
10. Set the **source adjustment**: add or remove a source, change a weight, reclassify (or invert) a source, add a
    signal, or add a regime condition. Press **Save & recalculate V1**.
11. Read **What would V1 become?** Impact is always *current V1 (reconstructed)* vs *proposed V1*, from the same stored
    readings with the same formula. *Stored V1* is the historical reference only.
    A brand-new source with no history shows **DATA COLLECTION REQUIRED**: no values are invented. You can still record
    and review the learning.
12. Read **Does it improve CryptoPulse?** It uses EXP-005's rule: V1 calls UP at 50 or above, and the outcome is BTC's
    direction 24h later. The verdict is one of NOT ENOUGH DATA, VALIDATING, SUPPORTED, NOT SUPPORTED or INCONCLUSIVE.
    A different number is never proof of improvement.
13. **Submit for review**, then **Approve V1 change**, **Reject** or **Investigate more**. Approving anything that is
    not SUPPORTED needs an explicit tick. Approval creates **v1.N = READY (not active)**. Production V1 does not change.

Writing needs the staging admin token (the `STAGE7_STAGING_ADMIN_TOKEN` GitHub secret value). Type it into the box at
the top right. It stays in that page only.

## When Claude is needed

Only for implementation. Example: ChatGPT and you confirm that "options open interest" should become a V1 source.
Claude then builds its data collection, so it can be recalculated, validated, and later activated through a separate,
explicitly authorized step.
