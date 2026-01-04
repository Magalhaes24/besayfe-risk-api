"""
Database-backed product source.

Reads product and allergen data from PostgreSQL using the project schema and
adapts it into the shared ProductInfo model for the risk engine.
"""

from __future__ import annotations

# Standard library typing helpers.
from typing import List, Optional

try:
    # psycopg2 is optional for environments that do not use the DB source.
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
    psycopg2 = None  # type: ignore
    RealDictCursor = None  # type: ignore

from .models import (
    AllergenFact,
    FacilityAllergenProfile,
    PresenceType,
    ProductInfo,
)
from .openfoodfacts_client import ProductDataSource


class DatabaseProductSource(ProductDataSource):
    """
    PostgreSQL-backed product source that mirrors the provided schema.
    """

    def __init__(self, dsn: str):
        # Guard against missing optional dependency.
        if psycopg2 is None:
            raise ModuleNotFoundError(
                "psycopg2 is required for DatabaseProductSource. Install via "
                "'pip install psycopg2-binary'."
            )
        # Store the database DSN for use in queries.
        self.dsn = dsn

    def get_product(self, ean: str) -> Optional[ProductInfo]:
        # Open a connection and fetch the product record by EAN.
        with psycopg2.connect(self.dsn, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, ean, name, brand, manufacturer_id, source
                    FROM products
                    WHERE ean = %s
                    """,
                    (ean,),
                )
                product_row = cur.fetchone()

            # Abort if no matching product exists in the DB.
            if not product_row:
                return None

            # Load related allergen facts and facility profiles.
            allergen_facts = self._fetch_allergen_facts(conn, product_row["id"])
            facilities = self._fetch_facility_profiles(conn, product_row["id"])
            # Add data notes for missing allergen facts.
            data_notes: List[str] = []
            if not allergen_facts:
                data_notes.append(
                    "No ingredient/allergen data found in database; cannot compute risk without supplemental data"
                )

            # Normalize the row into the shared ProductInfo model.
            return ProductInfo(
                ean=product_row["ean"],
                name=product_row["name"],
                brand=product_row.get("brand"),
                manufacturer_id=product_row.get("manufacturer_id"),
                source=product_row.get("source") or "db",
                allergen_facts=allergen_facts,
                facilities=facilities,
                raw_payload=product_row,
                data_notes=data_notes,
            )

    def _fetch_allergen_facts(
        self, conn, product_id: int
    ) -> List[AllergenFact]:
        # Query the product_allergen_facts table for this product.
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT allergen_code, presence_type, source, weight, confidence
                FROM product_allergen_facts
                WHERE product_id = %s
                """,
                (product_id,),
            )
            rows = cur.fetchall()

        # Convert rows into AllergenFact entries.
        facts: List[AllergenFact] = []
        for row in rows:
            facts.append(
                AllergenFact(
                    allergen_code=row["allergen_code"],
                    presence_type=PresenceType(row["presence_type"]),
                    source=row.get("source") or "db:product_allergen_facts",
                    weight=float(row["weight"]),
                    confidence=float(row["confidence"]),
                )
            )
        return facts

    def _fetch_facility_profiles(
        self, conn, product_id: int
    ) -> List[FacilityAllergenProfile]:
        # Query facility profiles linked to this product.
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT fap.facility_id,
                       fap.allergen_code,
                       fap.process_type,
                       fap.proportion_of_products
                FROM facility_products fp
                JOIN facility_allergen_profile fap ON fap.facility_id = fp.facility_id
                WHERE fp.product_id = %s
                """,
                (product_id,),
            )
            rows = cur.fetchall()

        # Convert rows into FacilityAllergenProfile entries.
        profiles: List[FacilityAllergenProfile] = []
        for row in rows:
            profiles.append(
                FacilityAllergenProfile(
                    facility_id=row["facility_id"],
                    allergen_code=row["allergen_code"],
                    process_type=row["process_type"],
                    proportion_of_products=(
                        float(row["proportion_of_products"])
                        if row["proportion_of_products"] is not None
                        else None
                    ),
                )
            )
        return profiles
