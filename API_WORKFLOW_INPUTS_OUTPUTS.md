# Allergen Risk API: How It Works

This document describes exactly how the current API behaves, including all inputs and outputs.

## Base URL

- Local: `http://localhost:8000`

## Authentication

- No authentication is currently required.

## Content Types

- `POST /risk`: `application/json`
- `POST /risk/image`: `multipart/form-data`

## Endpoints Overview

- `GET /` -> basic service status
- `GET /health` -> readiness check
- `GET /favicon.ico` -> placeholder response
- `POST /risk` -> barcode-based risk scoring (supports multiple allergen profiles)
- `POST /risk/image` -> OCR/image-based risk scoring (single allergen profile)

---

## 1) `GET /`

### Input

- No input.

### Output

```json
{
  "status": "ok",
  "service": "allergen-risk-api"
}
```

---

## 2) `GET /health`

### Input

- No input.

### Output

```json
{
  "status": "ok"
}
```

---

## 3) `GET /favicon.ico`

### Input

- No input.

### Output

```json
{
  "status": "ok"
}
```

---

## 4) `POST /risk` (Barcode-Based)

Computes risk from OpenFoodFacts product data using one or more allergen profiles.

### Input

- Method: `POST`
- Path: `/risk`
- Query params:
  - `include_raw` (boolean, optional, default: `true`)
    - If `true`, includes raw product payload under `product.raw`

- JSON body:

You can send either:

1. Multi-profile input (recommended)
2. Legacy single-profile input (backward compatible)

### 4.1 Multi-profile request body

```json
{
  "barcode": "737628064502",
  "allergen_profiles": [
    {
      "profile_id": "adult",
      "user_allergens": ["MILK", "GLUTEN"],
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

#### Fields

- `barcode` (string, required)
  - Product EAN/UPC barcode.
- `allergen_profiles` (array, optional but required if `user_allergens` is not provided)
  - Must contain 1 or more profiles when used.
- `allergen_profiles[].profile_id` (string, optional)
  - Client identifier returned as-is.
- `allergen_profiles[].user_allergens` (array of strings, required per profile)
  - One or more allergen codes.
  - Codes are normalized to uppercase.
- `allergen_profiles[].consider_may_contain` (boolean, optional, default: `true`)
- `allergen_profiles[].consider_facility` (boolean, optional, default: `false`)

### 4.2 Legacy single-profile request body

```json
{
  "barcode": "737628064502",
  "user_allergens": ["MILK", "GLUTEN"],
  "consider_may_contain": true,
  "consider_facility": false
}
```

#### Fields

- `barcode` (string, required)
- `user_allergens` (array of strings, optional if `allergen_profiles` provided)
  - One or more allergen codes.
  - Codes are normalized to uppercase.
- `consider_may_contain` (boolean, optional, default: `true`)
- `consider_facility` (boolean, optional, default: `false`)

### Validation rules

- Must provide either:
  - `allergen_profiles` with at least 1 profile, or
  - `user_allergens` with at least 1 allergen
- Each profile must have at least 1 allergen code.

### Output

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
    "ingredients_text": "...",
    "raw": {}
  },
  "cross_contact": {
    "PEANUT": {
      "risk": 0.21,
      "posterior_alpha": 2.1,
      "posterior_beta": 7.9,
      "n": 5,
      "k": 1
    }
  },
  "risk": {
    "per_allergen": {
      "MILK": { "score": 80.0, "profile_count": 1 },
      "GLUTEN": { "score": 60.0, "profile_count": 1 },
      "PEANUT": { "score": 40.0, "profile_count": 1 }
    },
    "final_score": 92.0,
    "combined": {
      "per_allergen": {
        "MILK": { "score": 80.0, "profile_count": 1 },
        "GLUTEN": { "score": 60.0, "profile_count": 1 },
        "PEANUT": { "score": 40.0, "profile_count": 1 }
      },
      "final_score": 92.0
    },
    "profiles": [
      {
        "profile_id": "adult",
        "user_allergens": ["MILK", "GLUTEN"],
        "consider_may_contain": true,
        "consider_facility": false,
        "cross_contact": {},
        "risk": {
          "per_allergen": {
            "MILK": { "score": 80.0, "reasons": ["..."] },
            "GLUTEN": { "score": 60.0, "reasons": ["..."] }
          },
          "final_score": 90.0
        }
      },
      {
        "profile_id": "child",
        "user_allergens": ["PEANUT"],
        "consider_may_contain": true,
        "consider_facility": true,
        "cross_contact": {
          "PEANUT": {
            "risk": 0.21,
            "posterior_alpha": 2.1,
            "posterior_beta": 7.9,
            "n": 5,
            "k": 1
          }
        },
        "risk": {
          "per_allergen": {
            "PEANUT": { "score": 40.0, "reasons": ["..."] }
          },
          "final_score": 40.0
        }
      }
    ]
  },
  "participant_scores": [
    {
      "profile_id": "adult",
      "final_score": 90.0,
      "allergens": ["MILK", "GLUTEN"],
      "per_allergen": {
        "MILK": { "score": 80.0, "reasons": ["..."] },
        "GLUTEN": { "score": 60.0, "reasons": ["..."] }
      }
    }
  ],
  "computed_overall_risk": 92.0,
  "displayed_overall_risk": 92.0,
  "summary": {
    "product": "Product name (737628064502)",
    "total_score": 90.0,
    "allergens_found": ["MILK:contains", "GLUTEN:may_contain"],
    "ingredients_text": "..."
  }
}
```

### Notes on `/risk` output

- `risk.profiles[]` always contains per-profile individual risk.
- `risk.combined` and top-level `risk.per_allergen` + `risk.final_score` represent the combined result across all input profiles.
- Top-level `cross_contact` is a merged view of per-profile `cross_contact` results.
- `summary.total_score` reflects the combined overall risk.
- Compatibility fields are also exposed at top-level and inside `risk`:
  - `participant_scores`
  - `computed_overall_risk`
  - `displayed_overall_risk`

### Errors

- `400 Bad Request`
  - Missing profile input (`allergen_profiles` and `user_allergens` both absent/empty)
  - Invalid allergen profiles (for example empty `user_allergens` in a profile)
- `404 Not Found`
  - Product not found on OpenFoodFacts
- `500 Internal Server Error`
  - Risk computation could not be completed

---

## 5) `POST /risk/image` (OCR-Based)

Computes risk from uploaded image text extraction.

Important: this endpoint currently supports a single allergen profile via comma-separated `user_allergens`.

### Input

- Method: `POST`
- Path: `/risk/image`
- Content-Type: `multipart/form-data`
- Form fields:
  - `file` (file, required)
    - Image file (e.g. jpg/png)
  - `user_allergens` (string, required)
    - Comma-separated allergen codes (example: `MILK,GLUTEN`)
  - `consider_may_contain` (boolean, optional, default: `true`)
  - `consider_facility` (boolean, optional, default: `false`)
  - `ocr_lang` (string, optional, default: `eng`)
  - `tesseract_cmd` (string, optional)
  - `reference_id` (string, optional)
  - `include_raw` (boolean, optional, default: `false`)

### Example cURL

```bash
curl.exe -X POST "http://localhost:8000/risk/image" \
  -F "file=@C:\path\to\label.jpg" \
  -F "user_allergens=MILK,GLUTEN" \
  -F "consider_may_contain=true" \
  -F "consider_facility=false"
```

### Output

```json
{
  "product": {
    "ean": "ocr:label.jpg",
    "name": "label.jpg",
    "brand": null,
    "source": "image_ocr",
    "data_notes": [],
    "allergens_tags": null,
    "traces_tags": null,
    "ingredients_text": "..."
  },
  "cross_contact": {},
  "risk": {
    "per_allergen": {
      "MILK": { "score": 70.0, "reasons": ["..."] },
      "GLUTEN": { "score": 30.0, "reasons": ["..."] }
    },
    "final_score": 79.0
  },
  "summary": {
    "product": "label.jpg (ocr:label.jpg)",
    "total_score": 79.0,
    "allergens_found": ["MILK:contains"],
    "ingredients_text": "..."
  }
}
```

### Errors

- `400 Bad Request`
  - Missing/empty `user_allergens`
  - Empty uploaded file
  - OCR failure
- `500 Internal Server Error`
  - Risk computation could not be completed

---

## How Risk Is Computed (High-Level)

1. Product is fetched (barcode) or extracted from OCR (image).
2. Allergen facts are gathered/enriched.
3. For each requested allergen in a profile:
   - direct/trace/facility signals are filtered by profile flags,
   - per-allergen score is computed,
   - fallback score is used if no direct evidence is found.
4. Per-allergen scores are aggregated into profile final score.
5. For multi-profile `/risk`, profile final scores are aggregated into combined final score.

Aggregation uses complementary probability so multiple signals increase risk without simple linear overcounting.

---

## Quick Testing Commands

### Health

```bash
curl.exe http://localhost:8000/health
```

### Barcode multi-profile

```bash
curl.exe -X POST "http://localhost:8000/risk" \
  -H "Content-Type: application/json" \
  -d "{\"barcode\":\"737628064502\",\"allergen_profiles\":[{\"profile_id\":\"adult\",\"user_allergens\":[\"MILK\",\"GLUTEN\"],\"consider_may_contain\":true,\"consider_facility\":false},{\"profile_id\":\"child\",\"user_allergens\":[\"PEANUT\"],\"consider_may_contain\":true,\"consider_facility\":true}]}"
```

### Barcode legacy single profile

```bash
curl.exe -X POST "http://localhost:8000/risk" \
  -H "Content-Type: application/json" \
  -d "{\"barcode\":\"737628064502\",\"user_allergens\":[\"MILK\",\"GLUTEN\"],\"consider_may_contain\":true,\"consider_facility\":false}"
```
