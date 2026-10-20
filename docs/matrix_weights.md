# Matrix weight spec (v2, 2026-10-20)

Weights are editable by ADMIN, must sum to 100, and are stored versioned in the DB (`weight_sets`).
Default: 25 / 25 / 25 / 25. Code: `engine/app/scoring.py` (`rank` = numpy, `rank_reference` = plain Python, tested equal).

## Cases: single vs shared
- pallet positions, floor spots, SHIP slots: **single**, one unit per case
- bins (`bin`) and shelving (`parcel`): **shared**, several units per case (`WH_SHARED_STORAGE_TYPES`)
  - usable volume = case volume x `fill_factor` (0.8: boxes never pack perfectly)
  - at most `max_skus_per_case` (3) different SKUs; the same SKU can always be added while there is room
  - case weight <= case max kg; the shelf-level limit (1250 kg) still applies

## Hard constraints (fail -> case excluded)
- case enabled, not reserved; single case empty / shared case has room (rules above)
- packaging compatible with storage type: pallet -> rack, parcel -> shelving/floor, small -> bin
- `L <= W_case + 2*overhang`, `W <= D_case + 2*overhang` (either orientation), `H <= H_case` (overhang = 50 mm, H never overhangs)
- `sum(level kg) + unit kg <= 1250`

No case passes -> `EXTRA`.

## Soft criteria (each normalized 0..1, higher = better)
| criterion | score |
|---|---|
| size fit | `0.4 space + 0.3 height + 0.3 keep` (below) |
| dwell time | short dwell (<= 7 d) -> near dock + low level preferred: `1 - (0.5 dock_norm + 0.5 level_norm)`; long dwell -> the inverse |
| fragility | fragility 0 -> 1.0 everywhere; fragility >= 1 -> `1 - level_norm` (low levels preferred) |
| weight vs capacity | heavy -> low: `1 - level_norm * (unit kg / 1250)` |

Size fit parts:
- **space**: single case = unit footprint / case footprint; shared case = (volume already in + unit) / usable volume, +0.1 if the same SKU is already there (fill existing bins first, keep a SKU together)
- **height**: unit height / case height (a low pallet in a tall slot wastes the height above it)
- **keep**: 1.0 if the case is in the lowest height class that fits the unit (within 5 cm); otherwise the share of available cases at least that tall. Rare tall slots score low, so they stay free for the goods that need them

`score = sum(w_i * s_i) / 100`, rounded to 4 decimals.

## Tie-break
1. lowest level
2. shortest distance to nearest dock
3. case ID ascending (natural)

## LIFO effect
Same-SKU arrivals: allocation to customer orders takes the newest unit first.

## Training
Grid search on weights (step 5) over historical `events`. Objective: mean pick travel distance + 10 * EXTRA rate. ADMIN applies the result manually. (Not built yet.)
