## Risk engine API

Python service that scores allergen risk (0-100, higher is riskier) for a product.
It supports barcode-based lookups via OpenFoodFacts and image-based OCR inputs.

### Requirements

- Python 3.10+
- Dependencies in `requirements.txt`
- Network access for OpenFoodFacts and OCR.space

Install dependencies:

```bash
pip install -r requirements.txt
```

### Running the API

```bash
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

Base URL: `http://localhost:8000`

### API overview

Endpoints:
- `GET /` basic health payload
- `GET /health` readiness probe
- `GET /favicon.ico` placeholder to avoid browser 404s
- `POST /risk` barcode-based risk scoring
- `POST /risk/image` OCR-based risk scoring

Response format:
- `product`: normalized product fields
- `risk`: per-allergen and total scores
- `cross_contact`: optional Bayesian cross-contact details
- `summary`: compact, human-friendly overview

### POST /risk (barcode-based)

Request body (JSON):

```json
{
  "barcode": "737628064502",
  "user_allergens": ["MILK", "GLUTEN"],
  "consider_may_contain": true,
  "consider_facility": false
}
```

Response (JSON):

```json
{
  "product": {
    "ean": "737628064502",
    "name": "Product name",
    "brand": "Brand",
    "source": "openfoodfacts",
    "data_notes": [],
    "allergens_tags": [],
    "traces_tags": [],
    "ingredients_text": "..."
  },
  "cross_contact": {},
  "risk": {
    "per_allergen": {
      "MILK": { "score": 80.0, "reasons": ["..."] },
      "GLUTEN": { "score": 60.0, "reasons": ["..."] }
    },
    "final_score": 90.0
  },
  "summary": {
    "product": "Product name (737628064502)",
    "total_score": 90.0,
    "allergens_found": ["MILK:contains", "GLUTEN:may_contain"],
    "ingredients_text": "..."
  }
}
```

Notes:
- If `consider_facility` is true, `cross_contact` includes Bayesian estimates.
- If the barcode is not found, the API returns `404`.

### POST /risk/image (OCR-based)

Multipart form fields:
- `file`: required image file (jpg/png)
- `user_allergens`: required comma-separated allergen codes (e.g. `MILK,GLUTEN`)
- `consider_may_contain`: optional (default `true`)
- `consider_facility`: optional (default `false`)
- `ocr_lang`: optional OCR language code (default `eng`)
- `reference_id`: optional identifier for the image (default filename)
- `include_raw`: optional (default `false`); include raw OCR payload in response

Example:

```bash
curl.exe -X POST "http://localhost:8000/risk/image" \
  -F "file=@C:\path\to\label.jpg" \
  -F "user_allergens=MILK,GLUTEN" \
  -F "consider_may_contain=true" \
  -F "consider_facility=false"
```

OCR notes:
- OCR is performed via OCR.space API (default key embedded in the code).
- You can override the key with `OCR_SPACE_API_KEY`.
- Raw OCR payload can be included with `include_raw=true`.

### CLI usage (barcode)

```bash
python main.py --ean 737628064502 --allergies MILK,GLUTEN --avoid-traces --avoid-facility-risk
```

Flags:
- `--allergies`: comma-separated allergen codes
- `--avoid-traces`: include `may_contain` signals in risk
- `--avoid-facility-risk`: include `facility_risk` signals in risk
- `--format`: `text` (default) or `json`
- `--db-dsn`: PostgreSQL DSN to use DB instead of OpenFoodFacts
- `--food-db`: optional path to FoodDB `Food.csv`

### CLI usage (interactive)

```bash
python simple_cli.py
```

The interactive CLI supports barcode or image input.

### Database mode

Install the DB dependency:

```bash
pip install psycopg2-binary
```

Run with a DSN:

```bash
python main.py --ean 0000000000000 --allergies MILK --avoid-traces --db-dsn "postgres://user:pass@localhost:5432/restriction_system"
```

### Architecture overview

- `risk_engine.models`: dataclasses for domain objects
- `risk_engine.openfoodfacts_client`: OpenFoodFacts fetch + normalization
- `risk_engine.risk_engine.RiskEngine`: core scoring pipeline
- `risk_engine.db_repository.DatabaseProductSource`: PostgreSQL adapter
- `risk_engine.image_ocr.ImageTextProductSource`: OCR.space integration
- `risk_engine.cross_contact_bhm`: Bayesian cross-contact model

### Important implementation details

- Allergen scoring uses complementary probability to avoid double counting.
- OCR-derived `contains` facts are treated as fully confident (`confidence=1.0`).
- OpenFoodFacts tags are normalized to internal codes in `OFF_TAG_TO_CODE`.
- Missing data is reported via `product.data_notes`.
