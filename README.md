# Besayfe Risk API

A Python service that scores allergen risk (0–100, higher = riskier) for a food product. It supports barcode lookups via OpenFoodFacts, PostgreSQL database queries, and image-based OCR from product labels.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Project Structure](#project-structure)
4. [Data Models](#data-models)
5. [Modules](#modules)
   - [risk_engine/models.py](#risk_enginemodelspy)
   - [risk_engine/allergens.py](#risk_engineallergenspy)
   - [risk_engine/openfoodfacts_client.py](#risk_engineopenfoodfacts_clientpy)
   - [risk_engine/db_repository.py](#risk_enginedb_repositorypy)
   - [risk_engine/image_ocr.py](#risk_engineimage_ocrpy)
   - [risk_engine/food_db.py](#risk_enginefood_dbpy)
   - [risk_engine/cross_contact_bhm.py](#risk_enginecross_contact_bhmpy)
   - [risk_engine/risk_engine.py](#risk_enginerisk_enginepy)
6. [Interfaces](#interfaces)
   - [REST API (api_server.py)](#rest-api-api_serverpy)
   - [CLI (main.py)](#cli-mainpy)
   - [Interactive CLI (simple_cli.py)](#interactive-cli-simple_clipy)
7. [Risk Scoring Algorithm](#risk-scoring-algorithm)
8. [Allergen Reference](#allergen-reference)
9. [Setup and Running](#setup-and-running)
10. [Configuration](#configuration)

---

## Overview

Besayfe Risk API helps people with food allergies assess whether a product is safe for them. Given a product (identified by barcode or a photo of its label) and a user's allergen profile, the engine returns a risk score per allergen and an overall total score.

Key capabilities:

- **Barcode lookup** — fetches product data from OpenFoodFacts or a local PostgreSQL database
- **Image OCR** — extracts ingredient text and contamination warnings from product label photos
- **Multi-profile support** — assess a product for multiple people (e.g., a family) in one request
- **Severity-aware scoring** — users can declare LOW/MEDIUM/HIGH sensitivity per allergen
- **Bayesian cross-contact modelling** — estimates facility contamination risk using a hierarchical model
- **FoodDB enrichment** — supplements allergen inference from a food category database

---

## Architecture

![Besayfe Risk API Architecture Diagram](diagrams/Besayfe%20Risk%20API%20Architecture.png)

**Text representation:**

```
┌────────────────────────────────────────────────────────────────┐
│                        Interfaces                              │
│   api_server.py (REST)   main.py (CLI)   simple_cli.py (REPL) │
└─────────────────────────────┬──────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│                       RiskEngine                               │
│  - Orchestrates data fetch, enrichment, and scoring            │
│  - assess(ean)  /  assess_product(ProductInfo)                 │
└──────┬────────────────┬──────────────┬─────────────────────────┘
       │                │              │
       ▼                ▼              ▼
┌──────────────┐ ┌────────────┐ ┌───────────────┐
│ ProductData  │ │  FoodDB    │ │  CrossContact │
│   Sources    │ │ Enrichment │ │     BHM       │
│              │ │            │ │               │
│ - OpenFood   │ │ food_db.py │ │cross_contact_ │
│   Facts      │ │            │ │   bhm.py      │
│ - PostgreSQL │ └────────────┘ └───────────────┘
│ - OCR Image  │
└──────────────┘
       │
       ▼
┌────────────────────────────────────────────────────────────────┐
│                        Domain Models                           │
│  ProductInfo · AllergenFact · UserAllergyProfile · RiskResult  │
└────────────────────────────────────────────────────────────────┘
```

**Data flow for a barcode request:**

1. Client sends barcode + user allergen profile
2. `RiskEngine.assess()` fetches `ProductInfo` from the injected data source
3. Engine attaches facility allergen profiles and optional FoodDB signals
4. For each allergen the user cares about, relevant `AllergenFact`s are collected
5. Proximity cross-contact facts and optional Bayesian facts are injected
6. Facts are scored and aggregated per allergen; then combined into a total score
7. `RiskResult` is returned to the interface layer for serialization

---

## Project Structure

```
besayfe-risk-api/
├── api_server.py              # FastAPI REST server
├── main.py                    # Full-featured CLI
├── simple_cli.py              # Interactive CLI (no flags needed)
├── requirements.txt
├── db/                        # Database migration scripts
├── docker/                    # Docker configuration
├── img/                       # Sample product label images
└── risk_engine/
    ├── __init__.py            # Public package exports
    ├── models.py              # Core domain dataclasses
    ├── allergens.py           # Annex II allergen definitions and detection
    ├── openfoodfacts_client.py# OpenFoodFacts HTTP client
    ├── db_repository.py       # PostgreSQL product source
    ├── image_ocr.py           # OCR pipeline (OCR.space API)
    ├── food_db.py             # FoodDB CSV enrichment
    ├── cross_contact_bhm.py   # Bayesian cross-contact model
    ├── risk_engine.py         # Core scoring engine
    └── allergen_labels.py     # Allergen label helper
```

---

## Data Models

Defined in `risk_engine/models.py`.

### `PresenceType`

Classifies how an allergen is present in a product:

| Value | Meaning |
|---|---|
| `CONTAINS` | Allergen is a declared ingredient |
| `MAY_CONTAIN` | "May contain" / traces warning |
| `FACILITY_RISK` | Inferred risk from facility cross-contact |

### `AllergySeverity`

How severe a user's reaction is to a specific allergen:

| Value | Score multiplier |
|---|---|
| `LOW` | × 0.5 — mild intolerance |
| `MEDIUM` | × 1.0 — default |
| `HIGH` | × 1.5 — anaphylaxis risk, capped at 100 |

### `AllergenFact`

A single evidence item that an allergen may be present. Produced by every data source and combined during scoring.

| Field | Type | Description |
|---|---|---|
| `allergen_code` | `str` | Canonical code e.g. `MILK`, `GLUTEN` |
| `presence_type` | `PresenceType` | How it is present |
| `source` | `str` | Where this fact came from (e.g. `openfoodfacts:allergens_tags`) |
| `weight` | `float` | Proportion of product implicated (0–1) |
| `confidence` | `float` | Data quality confidence (0–1) |

`normalized_score()` converts the fact into a 0–100 score:
- `CONTAINS`: `100 × weight × confidence`
- `MAY_CONTAIN`: `65 × weight × confidence`
- `FACILITY_RISK`: `100 × weight × confidence` (weight is the BHM probability)

### `ProductInfo`

Standardized product representation, source-agnostic.

| Field | Description |
|---|---|
| `ean` | Barcode or image identifier |
| `name` | Product name |
| `brand` | Brand name |
| `source` | Where data came from (`openfoodfacts`, `database`, `image_ocr`) |
| `allergen_facts` | List of `AllergenFact` objects |
| `facilities` | List of `FacilityAllergenProfile` objects |
| `raw_payload` | Source-specific extra data (varies by source) |
| `traces_tags` | Raw traces/may-contain tags from the source |
| `data_notes` | Diagnostic messages explaining missing or inconclusive data |

### `UserAllergyProfile`

Captures what a user wants to avoid and how sensitive they are.

| Field | Description |
|---|---|
| `allergen_codes` | Allergens the user must avoid |
| `avoid_traces` | Whether to consider `may_contain` signals |
| `avoid_facility_risk` | Whether to consider facility cross-contact |
| `allergen_severities` | Per-allergen `AllergySeverity` map (defaults to MEDIUM) |

### `RiskResult`

The final output of the engine.

| Field | Description |
|---|---|
| `total_score` | Overall 0–100 risk score |
| `product` | The `ProductInfo` that was assessed |
| `per_allergen` | `Dict[code, RiskDetail]` — per-allergen breakdown |

### `RiskDetail`

Per-allergen score with explanations.

| Field | Description |
|---|---|
| `allergen_code` | Allergen code |
| `score` | 0–100 score for this allergen |
| `reasons` | List of human-readable reason strings |
| `facts` | The `AllergenFact`s that contributed |
| `applied_severity` | The severity used for this allergen |

---

## Modules

### `risk_engine/models.py`

Pure domain dataclasses with no external dependencies. This is the shared contract used by all other modules.

---

### `risk_engine/allergens.py`

Defines the 14 EU Annex II allergens and provides free-form text matching.

**`ANNEX_II_ALLERGENS`** — dictionary keyed by canonical code (`GLUTEN`, `MILK`, etc.), containing:
- `en` / `pt` — human-readable labels
- `off_tags` — OpenFoodFacts tag aliases
- `keywords` — ingredient keywords in multiple languages

**`detect_allergens_in_ingredient_texts(texts)`** — tokenizes ingredient strings into unigrams, bigrams, and trigrams, then matches against the full synonym table (labels, tags, keywords, accent-normalized). Returns a sorted list of canonical allergen codes.

**`resolve_allergen_code(user_input)`** — maps any free-form allergen name (any language, accents stripped) to the canonical code. Returns `None` if unrecognised.

**`allergen_label(code, lang)`** — returns the human-readable label for a code in the requested language, falling back to English.

---

### `risk_engine/openfoodfacts_client.py`

Fetches and normalizes product data from the public OpenFoodFacts API.

**`ProductDataSource`** — abstract base class (interface) for all product data sources. Defines `get_product(ean) -> Optional[ProductInfo]`.

**`OpenFoodFactsClient`** — concrete implementation:
1. Calls `https://world.openfoodfacts.org/api/v2/product/{ean}`
2. Normalizes `allergens_tags` → `AllergenFact(CONTAINS)`
3. Normalizes `traces_tags` → `AllergenFact(MAY_CONTAIN)`
4. Runs `detect_allergens_in_ingredient_texts()` on ingredient text as a secondary signal
5. Deduplicates facts (tag-based facts take priority over keyword-inferred ones)
6. Records `data_notes` when allergen or ingredient data is missing

`raw_payload` for OpenFoodFacts products contains: `categories_tags`, `brands`, `ingredients_text`, `allergens_tags`, `traces_tags`, `category_stats`, `brand_stats`.

---

### `risk_engine/db_repository.py`

PostgreSQL-backed product source. Requires `psycopg2-binary`.

**`DatabaseProductSource`** — implements `ProductDataSource`:
1. Queries `products` table by EAN
2. Queries `product_allergen_facts` for declared allergens
3. Joins `facility_products → facility_allergen_profile` for facility data
4. Maps rows to `ProductInfo`, `AllergenFact`, and `FacilityAllergenProfile`

If no allergen facts are found in the database, a data note is added and the engine falls back to the conservative fallback score.

---

### `risk_engine/image_ocr.py`

Extracts ingredient and contamination data from product label images.

**`ImageTextProductSource`** — the OCR pipeline:

1. **Compression** (`_compress_image`) — if the image exceeds 900 KB, Pillow progressively reduces JPEG quality (85 → 60) and then scale (×0.85 per iteration) until it fits within the OCR.space free-tier limit of 1024 KB.

2. **OCR** (`_call_ocr_space`) — sends the image to the OCR.space API (Engine 2 / Tesseract). Returns `(text, error, payload)`. Errors are captured as `data_notes`.

3. **Text splitting** (`_split_text`) — normalizes the raw OCR output into a list of non-empty lines.

4. **Allergen detection** (`_facts_from_texts`) — runs `detect_allergens_in_ingredient_texts()` on **all** OCR lines to maximize allergen coverage.

5. **Section extraction** (`_extract_ingredient_sections`) — independently parses the same lines to extract **only** ingredient-relevant content for storage:
   - **`ingredients_text`**: lines collected after an "Ingredients:" / "Ingredientes:" header, stopped at section breaks (Nutrition, Storage, Best Before, etc.)
   - **`contamination_text`**: lines containing "contains", "may contain", "allergens", "traces", "Contém", "Pode conter", etc.

The `raw_payload` stored in `ProductInfo` contains only:
```python
{
    "ingredients_text": str | None,   # extracted ingredient list
    "contamination_text": str | None, # contamination declarations
    "ocr_space_error": str | None,    # OCR error message if any
}
```

The full raw OCR text and the OCR.space API response are intentionally discarded to keep stored data clean.

**Environment variable:** `OCR_SPACE_API_KEY` overrides the default embedded key.

---

### `risk_engine/food_db.py`

Optional enrichment layer using a FoodDB CSV export (`Food.csv`).

**`FoodDatabase`** — reads the CSV and builds an in-memory lookup. When `infer_allergen_facts()` is called with a product and a list of allergen codes, it:

1. Tokenizes the product's ingredient text
2. Matches tokens against `KEYWORD_RULES` — a dictionary mapping ingredient keywords to allergen codes with confidence weights
3. Also matches food category tags against known allergen categories
4. Emits `AllergenFact(CONTAINS)` entries only for allergens in the user's profile, avoiding noise
5. Skips dairy facts if plant-based milk keywords are detected (e.g. "oat milk", "almond milk")

`KEYWORD_RULES` covers all 14 Annex II allergens with curated keyword-to-weight mappings.

---

### `risk_engine/cross_contact_bhm.py`

Bayesian Hierarchical Model (BHM) for estimating facility cross-contact risk.

**`CrossContactConfig`** — model hyperparameters (priors for category, brand, and ingredient effects).

**`estimate_cross_contact(product, allergen_code)`** — computes P(cross-contact | product, allergen) using a logit-scale inference:
- Category effect: prior based on how common the allergen is in that food category
- Brand effect: prior based on brand-level allergen prevalence
- Ingredient signal: +boost if the allergen is directly declared
- May-contain boost: +boost if traces are declared

Returns a dict with `prob` (mean estimate) and `ci_95` (credible interval).

**`final_cross_contact_risk(product, allergen_code)`** — blends the BHM estimate with explicit ingredient and may-contain signals:

```
risk = min(1.0, ingredient_presence + 0.7 × may_contain + 0.5 × bhm_prob)
```

This is exposed to the `RiskEngine` via `_bhm_cross_contact_fact()` which wraps the result as a `FACILITY_RISK` `AllergenFact`.

---

### `risk_engine/risk_engine.py`

The central orchestrator.

**`RiskEngine`** dependencies injected at construction:
- `product_source`: any `ProductDataSource` implementation
- `facility_profiles`: optional list of `FacilityAllergenProfile`s
- `food_database`: optional `FoodDatabase`
- `fallback_score`: score used when no facts are found (default 5.0)

**`assess(ean, user_profile)`** — fetches by barcode then runs the pipeline.

**`assess_product(product, user_profile)`** — runs the pipeline on a pre-built `ProductInfo` (used by OCR flow).

**Pipeline (`_assess_product`):**

1. Attach facility allergen facts
2. Optionally enrich with FoodDB facts
3. For each allergen in the user's profile:
   - Collect matching facts filtered by user preferences (`avoid_traces`, `avoid_facility_risk`)
   - Add proximity cross-contact facts (see below)
   - Optionally add a BHM cross-contact fact
   - Aggregate facts into a per-allergen score
   - Apply severity multiplier
4. Aggregate per-allergen scores into total score

**Proximity triggers** — static rules that raise risk for related allergens when one is present:

| Trigger present | Raises risk for | Weight | Confidence | Type |
|---|---|---|---|---|
| `PEANUT` | `TREE_NUTS` | 1.0 | 1.0 | `CONTAINS` |
| `TREE_NUTS` | `PEANUT` | 0.8 | 0.85 | `MAY_CONTAIN` |

**Score aggregation** uses complementary probability to avoid overcounting multiple signals for the same allergen:

```
combined = 1 - ∏(1 - scoreᵢ/100)  × 100
```

---

## Interfaces

### REST API (`api_server.py`)

Start the server:

```bash
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

#### `GET /health`
Readiness probe. Returns `{"status": "ok"}`.

---

#### `POST /risk` — Barcode-based assessment

**Request body (JSON):**

```json
{
  "barcode": "737628064502",
  "allergen_profiles": [
    {
      "profile_id": "adult",
      "user_allergens": ["MILK", "GLUTEN"],
      "allergen_severities": { "MILK": "HIGH", "GLUTEN": "MEDIUM" },
      "consider_may_contain": true,
      "consider_facility": false
    },
    {
      "profile_id": "child",
      "user_allergens": ["PEANUT"],
      "consider_may_contain": true,
      "consider_facility": true
    }
  ]
}
```

Single-profile shorthand (backward compatible):

```json
{
  "barcode": "737628064502",
  "user_allergens": ["MILK", "GLUTEN"],
  "consider_may_contain": true
}
```

**Response (JSON):**

```json
{
  "product": {
    "ean": "737628064502",
    "name": "...",
    "brand": "...",
    "source": "openfoodfacts",
    "data_notes": [],
    "allergens_tags": [],
    "traces_tags": [],
    "ingredients_text": "..."
  },
  "cross_contact": {},
  "risk": {
    "per_allergen": {
      "MILK": { "score": 80.0, "profile_count": 1 },
      "GLUTEN": { "score": 60.0, "profile_count": 1 }
    },
    "final_score": 88.0,
    "profiles": [
      {
        "profile_id": "adult",
        "risk": {
          "per_allergen": { "MILK": { "score": 80.0, "reasons": ["..."] } },
          "final_score": 80.0
        }
      }
    ]
  },
  "summary": {
    "product": "Product Name (737628064502)",
    "total_score": 88.0,
    "allergens_found": ["MILK:contains", "GLUTEN:may_contain"]
  }
}
```

Returns `404` if the product is not found.

---

#### `POST /risk/image` — OCR-based assessment

Multipart form fields:

| Field | Required | Default | Description |
|---|---|---|---|
| `file` | yes | — | Image file (jpg/png) |
| `user_allergens` | yes | — | Comma-separated codes: `MILK,GLUTEN` |
| `consider_may_contain` | no | `true` | Include may-contain signals |
| `consider_facility` | no | `false` | Include facility cross-contact |
| `ocr_lang` | no | `eng` | OCR language code (`por`, `fra`, etc.) |
| `reference_id` | no | filename | Identifier for the image |
| `include_raw` | no | `false` | Include `ingredients_text` / `contamination_text` in response |
| `allergen_severities` | no | — | JSON: `{"MILK":"HIGH","GLUTEN":"LOW"}` |

Example with curl (Windows):

```bash
curl.exe -X POST "http://localhost:8000/risk/image" ^
  -F "file=@C:\path\to\label.jpg" ^
  -F "user_allergens=MILK,GLUTEN,SOY" ^
  -F "consider_may_contain=true" ^
  -F "ocr_lang=por" ^
  -F "allergen_severities={\"MILK\":\"HIGH\"}"
```

---

### CLI (`main.py`)

Full-featured command-line interface.

```bash
python main.py --ean 737628064502 --allergies MILK,GLUTEN --avoid-traces
```

**Flags:**

| Flag | Description |
|---|---|
| `--ean` | Product barcode |
| `--allergies` | Comma-separated allergen codes |
| `--avoid-traces` | Consider may-contain signals |
| `--avoid-facility-risk` | Consider facility cross-contact signals |
| `--format text\|json` | Output format (default: `text`) |
| `--lang en\|pt` | Output language (default: `en`) |
| `--db-dsn` | PostgreSQL DSN — uses DB instead of OpenFoodFacts |
| `--food-db` | Path to FoodDB `Food.csv` for enrichment |
| `--severity` | Per-allergen severity: `MILK:HIGH,GLUTEN:LOW` |

Text output renders a visual bar chart with per-allergen scores and a total risk rating (Very Low → Very High).

History is appended to `risk_history.csv` on each run.

---

### Interactive CLI (`simple_cli.py`)

No flags needed — prompts for everything:

```bash
python simple_cli.py
```

Steps:
1. Language selection (`en` / `pt`)
2. Input mode: `barcode` or `image`
3. Barcode entry or image file path
4. Allergen selection from numbered Annex II list
5. May-contain and facility risk preferences
6. Displays the risk report

---

## Risk Scoring Algorithm

### Per-allergen score

For each allergen in the user's profile:

1. Collect all `AllergenFact`s for that allergen, filtered by user preferences
2. Compute `normalized_score()` per fact (0–100)
3. Aggregate with **complementary probability**:
   ```
   score = (1 - ∏(1 - sᵢ/100)) × 100
   ```
   This avoids overcounting when multiple sources independently confirm the same allergen.
4. Multiply by severity: `LOW` × 0.5, `MEDIUM` × 1.0, `HIGH` × 1.5, capped at 100
5. If no facts found: use `fallback_score` (default 5.0) — a conservative non-zero signal

### Total score

The per-allergen scores are combined again using complementary probability:

```
total = (1 - ∏(1 - allergen_scoreᵢ/100)) × 100
```

### Multi-profile scoring

When multiple allergen profiles are submitted:
1. Each profile is scored independently against a fresh copy of the product
2. Per-allergen scores across profiles are combined with complementary probability
3. The `combined` block in the response shows the merged view

---

## Allergen Reference

All 14 EU Annex II allergens supported:

| Code | English Name |
|---|---|
| `GLUTEN` | Cereals containing gluten (wheat, rye, barley, oats) |
| `CRUSTACEANS` | Crustaceans (shrimp, crab, lobster) |
| `EGG` | Eggs |
| `FISH` | Fish |
| `PEANUT` | Peanuts |
| `SOY` | Soybeans |
| `MILK` | Milk and dairy (including lactose) |
| `TREE_NUTS` | Tree nuts (almond, hazelnut, walnut, cashew, pistachio…) |
| `CELERY` | Celery |
| `MUSTARD` | Mustard |
| `SESAME` | Sesame seeds |
| `SULPHITES` | Sulphur dioxide and sulphites (>10 mg/kg) |
| `LUPIN` | Lupin |
| `MOLLUSCS` | Molluscs (clams, mussels, oysters, squid…) |

---

## Setup and Running

**Requirements:** Python 3.10+, network access for OpenFoodFacts and OCR.space.

```bash
pip install -r requirements.txt

# Optional: PostgreSQL support
pip install psycopg2-binary

# Run the API server
uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload

# Run the CLI
python main.py --ean 737628064502 --allergies MILK,GLUTEN --avoid-traces

# Run the interactive CLI
python simple_cli.py
```

---

## Configuration

| Environment variable | Description | Default |
|---|---|---|
| `OCR_SPACE_API_KEY` | OCR.space API key | Embedded default key |

The FoodDB path and PostgreSQL DSN are passed as CLI flags or constructor arguments — they are not environment variables.

**OCR size limit:** OCR.space free tier accepts images up to 1024 KB. The engine automatically compresses images exceeding 900 KB using Pillow before sending them.
