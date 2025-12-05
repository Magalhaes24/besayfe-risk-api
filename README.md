## Risk engine prototype

Python OOP prototype that scores allergen risk (0-100, higher is riskier) for a product
given a user's allergy profile. Data is fetched from OpenFoodFacts, normalized to the
schema described in the prompt, and evaluated using configurable rules.

### Quick start

```bash
pip install requests
python main.py --ean 737628064502 --allergies MILK,GLUTEN --avoid-traces --avoid-facility-risk
```

Flags:
- `--allergies`: comma-separated allergen codes (matches the `allergens` table codes)
- `--avoid-traces`: include `may_contain` signals in risk
- `--avoid-facility-risk`: include `facility_risk` signals in risk
- `--format`: `text` (default) pretty console view or `json` for raw output
- `--db-dsn`: PostgreSQL DSN to read from your database instead of OpenFoodFacts (e.g. `postgres://user:pass@host:5432/dbname`)

For database mode, install the extra dependency:

```bash
pip install psycopg2-binary
python main.py --ean 0000000000000 --allergies MILK --avoid-traces --db-dsn "postgres://user:pass@localhost:5432/restriction_system"
```

### Architecture (OOP)

- `risk_engine.models` — dataclasses mirroring the database concepts:
  - `AllergenFact`, `FacilityAllergenProfile`, `ProductInfo`, `UserAllergyProfile`,
    `RiskResult`.
- `risk_engine.openfoodfacts_client` — `ProductDataSource` interface and
  `OpenFoodFactsClient` implementation to fetch by EAN and map OpenFoodFacts tags to
  internal allergen codes.
- `risk_engine.risk_engine.RiskEngine` — orchestrates fetch → normalize → score.
  Uses complementary probability to combine multiple signals and avoid double
  counting. `AllergenFact.normalized_score` encodes the severity for `contains`,
  `may_contain`, and `facility_risk`.
- `risk_engine.db_repository.DatabaseProductSource` — fetches `ProductInfo` from
  PostgreSQL using the schema from the prompt (products, product_allergen_facts,
  facility_products, facility_allergen_profile).

### Extending toward the database schema

- Database repository is included; supply `--db-dsn` to use it. Extend queries if
  you add more metadata columns.
- Persist outputs into `risk_logs` by serializing `RiskResult`.

### Notes

- The OpenFoodFacts mapping in `OpenFoodFactsClient.OFF_TO_INTERNAL` should be
  extended as new allergens are added to the database.
- Network failures return `None`; callers should handle missing products gracefully.
