# PRD: warehouse slotting system (v1)

Sources: the original brief and the Q&A of 2026-10-17 (kept local), the FreeCAD build script.
Tags: **[user]** = answered by user. **[default]** = chosen by Claude (no answer given), editable.

---

## 0. Conflicts to confirm (answers contradict each other or the model)

| # | Conflict | Decision taken (until told otherwise) |
|---|---|---|
| C1 | Q1 ID `A20S2` vs Q2 granularity = single pallet position. `A20S2` only names a shelf. Its levels and pallet positions would collide | Keep `A{aisle}S{shelf}` as the shelf ID [user], add level + position for the case: `A20S2L1P2`. Aisle numbers are unique site-wide (no building prefix) |
| C2 | Q4 says "large shelves hold 2 pallets". The model has B2 bays of 2.7 m = 3 EUR pallets (800 wide) | Use the user's answer: bay >= 2.7 m -> 2 positions, smaller -> 1. Configurable per rack type in `config/storage_types.yaml` |
| C3 | Q5 1250 kg per shelf (level) and Q11 1250 kg max per pallet. One max pallet fills a 2-position level | Hard constraint on the sum of pallet weights per level <= 1250 kg. A second pallet is refused if the sum exceeds it |
| C4 | Q16 says no physical confirmation, but Q31 says yes to "scan shelf -> confirm" | Operator taps **Placed** (required, one tap). Shelf-label scan is optional: if scanned and it mismatches -> warning. Without "Placed", the slot stays RESERVED |
| C5 | Q15 says no audit trail, but Q19 tunes weights from history | No user attribution. An anonymous `events` table (placed/freed/staged/shipped + timestamps) is kept, because training and SSE both need it |
| C6 | Q22 "UTC+2" is only correct in summer (CEST). It is UTC+1 from 2026-10-25 | Store UTC, display `Europe/Rome` (DST-aware) |
| C7 | Q14 mentions only an ADMIN role, but Q17 approvals imply operators too | 2 roles: OPERATOR + ADMIN, both with PIN login |

---

## 1. Goal
New delivery -> instant case ID (or `EXTRA`) that optimizes storage and speeds handling. Live sync from the Excel files. Pick + truck views.

## 2. Out of scope v1
- write-back to Excel
- VLMs
- cycle counts
- user audit trail
- ML prediction
- WMS/ERP integration (Q38 TBD)
- success metrics (Q40)

---

## 3. Physical model

### 3.1 Storage types (Q3) [default]
| type (FreeCAD prefix) | slottable v1 | content |
|---|---|---|
| `B2_pallet_rack_*` | yes (pilot) | EUR pallets |
| `B3_rack_*` | yes | EUR pallets, bulky |
| `B4_rack*` (high-bay) | yes | EUR pallets |
| `B1_rack_wall_W`, `B2_rack_wall_W`, `B4_rack_wall_W` | yes | EUR pallets |
| `B1_shelf_tall_*`, `B1_shelf_lowGF_*` | yes | parcels without pallet |
| `B1_UF_smallparts_*` (mezzanine) | yes | small items in plastic bins (Q11) |
| B3 floor storage | yes, as zone `FLOOR{n}` | parcels without pallet (Q11) |
| VLMs | no | - |
| SHIP | logical only, not in FreeCAD (Q21) | STAGED articles |

### 3.2 IDs (Q1, C1)
- shelf: `A{aisle}S{shelf}`, no zero padding [user], e.g. `A20S2`
- case: `A{aisle}S{shelf}L{level}P{pos}`, e.g. `A20S2L1P2`. Level 1 = floor
- aisle numbering: one aisle per rack row, in fixed order: building B1->B4, then rack family, then position across the row. Deterministic, never edited by hand (88 rows, 18,707 cases + 35 floor spots in the current model)
- special: `EXTRA` (unassignable), `SHIP{n}` (shipping area, default 40 slots [default]), `FLOOR{n}`
- mapping is written once by the macro to `data/storage_spaces.json`, then imported to the DB. Any rerun must yield an identical file (CI check)

### 3.3 Capacity (Q4, Q5, Q11)
- one case = one pallet position [user]
- shelf limit: 1250 kg per level [user]. Single pallet max: 1250 kg [user]
- fit check: pallet L x W x H (incl. 144 mm EUR base) vs free envelope (bay width / positions, depth, level clearance)
- overhang allowed [user]: tolerance 50 mm per side [default]
- capacity unit stored = m3 volume [user] + kg. Pallet-position count is derived

### 3.4 FreeCAD work
- target: the working FreeCAD model (`freecad/warehouse_model.FCStd`)
- empty shelves (Q12): remove the pallet/load solids from the rack compounds. Approach: rerun the build script with `fill=0`, then reapply `open_passages`, `clear_aisles`, `color_and_stair`. Fallback: strip load solids in place
- macro outputs, per case: ID, building, type, xyz, envelope dims, m3, kg limit, plan rectangle (for the 2D map)
- headless only: `freecadcmd`
- on-site survey of rack dims before go-live (Q39) -> update the `P` params and rerun

---

## 4. Slotting engine

### 4.1 Inventory method (Q6)
LIFO [user]. Last received leaves first. Effect on slotting: new arrivals of an SKU go to the most accessible position (lowest level, closest to the aisle end nearest the dock).

### 4.2 Dimension chain (Q10)
1. PO
2. delivery note
3. supplier site
4. web scrape
5. manual entry on the dropoff screen

- every result is stored with its `source` + confidence
- scraping only where ToS allows. Per-supplier adapter, off by default

### 4.3 Article attributes (Q8)
- fragility: only if the supplier states it (site / delivery note) [user]. Scale 0-2 (0 none, 1 fragile, 2 very fragile) [default]. Default 0
- dwell time: default 14 days [default], until CPO history exists
- unit of slotting: PO line [user]. A line with N pallets -> N cases, same shelf preferred

### 4.4 Matrix (Q7)
- hard constraints (must pass):
  - case FREE and not RESERVED
  - dims fit (incl. overhang tolerance)
  - level weight sum <= 1250 kg
  - storage type matches the packaging (pallet / parcel / bin)
- soft score = weighted sum, weights 25 / 25 / 25 / 25 [default], editable by ADMIN:
  - size fit
  - dwell time
  - fragility
  - weight vs capacity
- tie-break: lowest level [user], then distance to the nearest dock, then case ID ascending
- no candidates -> `EXTRA`
- full formulas: `docs/matrix_weights.md`

### 4.5 Reservation (Q23)
- a suggestion reserves the case for 10 min [default]
- "Placed" -> OCCUPIED. Timeout -> FREE

### 4.6 Training (Q19)
- tune weights from history [user]
- target metric [default]:
  - minimize mean pick travel distance
  - minimize EXTRA rate
- offline grid search over the `events` table, run on demand by ADMIN. No auto-apply

### 4.7 24h pre-ship rule
As in `consignes.txt`. Ship date without an hour -> 08:00 Europe/Rome [default].

### 4.8 Endpoints
As in `consignes.txt`, plus:
- `POST /articles/{id}/placed`
- `POST /moves` (request) + `POST /moves/{id}/approve` (ADMIN, Q17)
- `GET /events` (SSE)
- `GET /map`
- `POST /auth/pin`

---

## 5. Ingestion
- Excel files are local on the Linux server (Q13) -> watchdog event mode
- polling stays available per path in `config/live_sync.yaml`
- rest as in `consignes.txt` (idempotency, dead-letter, last-write-wins)
- sample data (Q9): synthetic, generated into `data/samples/`:
  - supplier PO
  - delivery note
  - customer PO
  - truck planning
  - generator script committed
  - Italian-style supplier names, EUR pallets, mm + kg, ISO dates

---

## 6. Operator UI

### 6.1 Device + input (Q27, Q29, Q30)
- tablets [user], English [user]
- scanning [default]: camera (tablet) + keyboard-wedge input both supported. Formats: Code128, EAN-13, QR
- Wi-Fi assumed [default]. No offline mode. "Offline" banner + actions blocked when disconnected
- big buttons (min 64 px targets), landscape + portrait

### 6.2 Labels (Q28)
- generate printable shelf/case labels: PDF, A4 sheet of labels
- content: Code128 + human-readable ID + big level number
- [default] printer: any office printer

### 6.3 Pages
- **dropoff** (Q31): scan article -> slot + 2D map -> go -> (optional shelf scan) -> **Placed**
  - manual-dims form if the chain failed
  - `EXTRA` -> "Set aside, manager notified"
- **pick** (Q33): one order at a time [user]
  - shortest-path ordering [user]: nearest-neighbour over aisle graph from the map [default]
  - STAGED "to load" list
- **truck planner** (Q20): schedule only [user]
  - docks: 6 east hall + 4 north hall [user]
  - STAGED = manifest
- **admin** (Q34): weights (no preview [user]), shelf overrides, move approvals, EXTRA queue (manager resolves one by one, Q32), label printing
- **map** (Q35): 2D SVG plan from macro rectangles, target case highlighted, live occupancy colours

### 6.4 Feedback (Q36)
No special constraints. Default: beep + colour flash on scan.

---

## 7. Infra + ops [default unless noted]
- on-prem Linux server [user]
- 3 services:
  - engine
  - watcher
  - UI
- run via docker compose or systemd units
- auth: PIN, 2 roles (C7). PINs hashed (argon2)
- backups: nightly SQLite `.backup` -> `/var/backups/warehouse/`, keep 30 days
- Postgres migration when either:
  - more than ~10 concurrent writers, or
  - DB > 5 GB
- monitoring:
  - `GET /health`
  - logs to journald
  - dead-letter count shown on the admin page
- fallback when down (Q42): nightly PDF "free cases by type", printed. Manager assigns by hand, data re-entered after recovery
- MCP (Q25):
  - FreeCAD: build a minimal stdio MCP wrapping `freecadcmd` (run macro, list objects, query a case). The headless rule excludes GUI-addon MCPs
  - DB: SQLite MCP server. Exact package chosen and verified at setup
- repo: `git init` at `warehouse/` [user]. No remote yet. FreeCAD folder -> `freecad/` (per layout)

---

## 8. Sizing assumptions (Q37 blank) [default]
- 100 inbound pallets/day, peak 250
- 150 outbound order lines/day
- 5,000 SKUs
- 6 operators/shift
- SQLite adequate at this scale

---

## 9. MVP + milestones (Q41) [default]
1. git init, repo layout, CLAUDE.md, empty skeleton deployed (3 services up, health green)
2. FreeCAD: empty shelves + macro -> `storage_spaces.json` + spreadsheet report (sizes/volumes, grouped by capacity)
3. schema + DB import of cases
4. engine scoring + `/slot` + tests
5. **pilot: B2 only** (dropoff + map + Placed)
6. watcher + live Excel sync + SSE
7. pick + 24h rule + SHIP
8. truck planner
9. admin + labels + training
10. extend to B1, B3, B4

---

## 10. Build status (2026-10-17)
Done:
- FreeCAD: shelves emptied, export + xlsx report
- engine: schema, import, matrix, slotting, ingest (Excel + JSON), dimension chain, 24h rule, LIFO allocation, pick/load, trucks, SSE, admin API. 35 tests
- watcher: debounce, retry/backoff, rename grace. 11 tests
- UI: drop-off, pick, trucks, find/map, admin (weights, EXTRA, overrides, dead letter), shelf labels

Not done yet:
- PIN auth + roles (C7)
- move requests with admin approval (Q17)
- weight training from history (Q19)
- supplier-site / scrape adapters (registry exists, none configured)
- MCP servers (Q25)
- deploy units (systemd/compose)
- nightly backup

## 11. Spreadsheet mode (2026-10-18, user request)
- the team works from one status spreadsheet instead of the localhost UI: Ordered / In stock (+ freshly received) / Shipments, case # on every stored unit
- receipt = delivery-note line; the engine assigns and books the case on receipt (no operator confirmation, consistent with Q16)
- tablet UI kept as an option (`WH_AUTO_PLACE_ON_RECEIPT=false`)

## 12. Fit improvements (2026-10-20)
- height-aware size fit + scarcity ("keep"): low pallets no longer take the rare tall slots, which removes most tall-pallet EXTRA
- shared cases: bins and shelving hold several units (usable volume 80%, max 3 SKUs per case); pallet positions stay one pallet each
- numpy ranking (same results as the reference implementation, ~4x faster)
- spreadsheet In stock: "Units in case" and "Case fill %"

## 13. Input window (2026-10-20)
- Windows XP error-box look (Tkinter, `scripts/window.sh`): three data-sheet slots (Ordered / In stock / Shipments), pdf / xlsx / xlsm / csv
- the engine inspects each file; a workbook holding several datasets is one database for all of them, imported once
- quick parameters (applied live, persisted): correlation weights, assign case on delivery note, buildings in use,
  release before shipping, default dwell, "New add-on" window (hours a delivery stays marked new)
- removed from the window 2026-10-20 (fixed at their defaults, still `WH_*` env settings): shared-case usable volume,
  max SKUs per bin, overhang, fast-mover threshold
- other parameters proposed, not built: reservation time (tablet mode), export folder, rename grace, default ship hour,
  pallet profile (EUR / ISO), per-supplier dimension sources
