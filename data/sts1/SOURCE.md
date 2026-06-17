# StS1 card / relic reference data

`cards.json` (360 cards) and `relics.json` (181 relics) are vendored from
**spire-archive** by nkhoit — parsed directly from the Slay the Spire 1 game JAR:

- https://github.com/nkhoit/spire-archive  (`data/sts1/`)

The bridge ([sts_advisor.py](../../sts_advisor.py)) joins these to the live
CommunicationMod game state by card/relic `id`, **uppercased** — CommunicationMod's
ids are the lowercase/mixed-case form of these (`Strike_R` → `STRIKE_R`, `Bash` →
`BASH`). The matched effect text (base + upgraded) is injected into the prompt as a
`CARD REFERENCE` / `RELIC REFERENCE` block so the advisor reasons from real text
instead of memory.

To refresh, re-download those two files from the upstream repo. Base-game (vanilla)
StS1 only — modded / StS2 cards won't be found and fall back to the prompt's
"from memory" handling.
