import json
import os
from pathlib import Path
import unittest

os.environ.setdefault("MAX_PAGES", "10")
os.environ.pop("AZURE_STORAGE_CONNECTION_STRING", None)

from services.sos_client import build_search_body
from services.sync_engine import (
    PROJECT_ROOT,
    TAXON_EXTENSION_FIELDS,
    get_full_refresh_reason,
    normalize_record,
    should_do_full_refresh,
    taxon_family_swedish_name,
    with_taxon_extension_fields,
)


class SosTaxonMappingTests(unittest.TestCase):
    def test_search_body_requests_systematic_taxon_fields(self):
        body = build_search_body(
            "2026-06-22T00:00:00",
            "2026-06-22T23:59:59",
        )

        fields = body["output"]["fields"]

        for field in [
            "taxon.scientificName",
            "taxon.taxonRank",
            "taxon.order",
            "taxon.family",
            "taxon.genus",
            "taxon.attributes.sortOrder",
            "taxon.attributes.dyntaxaTaxonId",
        ]:
            self.assertIn(field, fields)

    def test_normalize_record_maps_taxon_sort_order_for_bird_observation(self):
        normalized = normalize_record(
            {
                "occurrence": {
                    "occurrenceId": "urn:lsid:artportalen.se:sighting:134558205",
                    "individualCount": "2",
                    "reportedDate": "2026-06-22T00:01:56.877+02:00",
                    "url": "https://www.artportalen.se/sighting/134558205",
                },
                "taxon": {
                    "id": 103004,
                    "vernacularName": "savsangare",
                    "scientificName": "Acrocephalus schoenobaenus",
                    "taxonRank": "Species",
                    "order": "Passeriformes",
                    "family": "Acrocephalidae",
                    "genus": "Acrocephalus",
                    "attributes": {
                        "redlistCategory": "LC",
                        "sortOrder": 12345,
                        "dyntaxaTaxonId": 103004,
                    },
                },
                "location": {
                    "decimalLatitude": 56.75108,
                    "decimalLongitude": 14.5815,
                    "municipality": {"name": "Alvesta"},
                    "county": {"name": "Kronoberg"},
                },
                "event": {
                    "startDate": "2026-06-22T00:01:00+02:00",
                },
                "modified": "2026-06-22T00:02:01.733+02:00",
            }
        )

        self.assertEqual(normalized["taxonSortOrder"], 12345)
        self.assertEqual(normalized["taxonDyntaxaId"], 103004)
        self.assertEqual(normalized["taxonOrder"], "Passeriformes")
        self.assertEqual(normalized["taxonFamily"], "Acrocephalidae")
        self.assertEqual(normalized["taxonGenus"], "Acrocephalus")
        self.assertEqual(
            normalized["taxonScientificName"],
            "Acrocephalus schoenobaenus",
        )
        self.assertEqual(normalized["taxonRank"], "Species")
        self.assertEqual(normalized["scientificName"], "Acrocephalus schoenobaenus")
        self.assertEqual(normalized["commonName"], "savsangare")

    def test_normalize_record_maps_laridae_to_swedish_family_name(self):
        normalized = normalize_record(
            {
                "occurrence": {"occurrenceId": "laridae"},
                "taxon": {
                    "family": "Laridae",
                    "scientificName": "Larus canus",
                },
                "location": {},
                "event": {},
            }
        )

        self.assertEqual(normalized["taxonFamily"], "Laridae")
        self.assertEqual(normalized["taxonFamilySwedish"], "Måsar och trutar")

    def test_normalize_record_sets_unknown_family_swedish_name_to_none(self):
        normalized = normalize_record(
            {
                "occurrence": {"occurrenceId": "unknown-family"},
                "taxon": {
                    "family": "Unknownidae",
                    "scientificName": "Aves species",
                },
                "location": {},
                "event": {},
            }
        )

        self.assertEqual(normalized["taxonFamily"], "Unknownidae")
        self.assertIsNone(normalized["taxonFamilySwedish"])

    def test_normalize_record_keeps_taxon_fields_null_safe(self):
        normalized = normalize_record(
            {
                "occurrence": {
                    "occurrenceId": "missing-taxon-fields",
                },
                "taxon": {
                    "scientificName": "Aves species",
                    "attributes": None,
                },
                "location": {
                    "municipality": None,
                    "county": None,
                },
                "event": {},
            }
        )

        for field in [
            "taxonSortOrder",
            "taxonDyntaxaId",
            "taxonOrder",
            "taxonFamily",
            "taxonFamilySwedish",
            "taxonGenus",
            "taxonRank",
            "municipality",
            "county",
        ]:
            self.assertIsNone(normalized[field])

        self.assertEqual(normalized["taxonScientificName"], "Aves species")

    def test_normalize_record_sets_missing_or_null_family_swedish_name_to_none(self):
        for taxon in [{"scientificName": "Aves species"}, {"family": None}]:
            with self.subTest(taxon=taxon):
                normalized = normalize_record(
                    {
                        "occurrence": {"occurrenceId": "missing-or-null-family"},
                        "taxon": taxon,
                        "location": {},
                        "event": {},
                    }
                )

                self.assertIsNone(normalized["taxonFamily"])
                self.assertIsNone(normalized["taxonFamilySwedish"])

    def test_cached_records_without_taxon_fields_trigger_full_refresh(self):
        existing_today = {
            "records": [
                {
                    "id": "old-shape",
                    "taxonId": 103077,
                    "scientificName": "Acrocephalus dumetorum",
                }
            ]
        }
        existing_syncstate = {
            "dateFilter": {"startDate": "2026-06-22T00:00:00"},
            "latestSourceModifiedAt": "2026-06-22T10:00:00+02:00",
        }

        self.assertTrue(
            should_do_full_refresh(
                existing_today,
                existing_syncstate,
                "2026-06-22T00:00:00",
            )
        )

    def test_cached_records_missing_taxon_family_swedish_trigger_full_refresh(self):
        complete_record = with_taxon_extension_fields(
            {
                "id": "old-cache",
                "taxonId": 103077,
                "taxonSortOrder": 58199,
                "taxonFamily": "Laridae",
            }
        )
        existing_syncstate = {
            "dateFilter": {"startDate": "2026-06-22T00:00:00"},
            "latestSourceModifiedAt": "2026-06-22T10:00:00+02:00",
        }

        for missing_field in ("taxonSortOrder", "taxonFamilySwedish"):
            with self.subTest(missing_field=missing_field):
                cached_record = dict(complete_record)
                del cached_record[missing_field]

                reason = get_full_refresh_reason(
                    {"records": [cached_record]},
                    existing_syncstate,
                    "2026-06-22T00:00:00",
                )

                self.assertEqual(reason, "missing_taxon_extension_fields")

    def test_cached_records_with_taxon_fields_can_use_delta_sync(self):
        existing_today = {
            "records": [
                with_taxon_extension_fields(
                    {
                        "id": "new-shape",
                        "taxonId": 103077,
                        "taxonSortOrder": 58199,
                    }
                )
            ]
        }
        existing_syncstate = {
            "dateFilter": {"startDate": "2026-06-22T00:00:00"},
            "latestSourceModifiedAt": "2026-06-22T10:00:00+02:00",
        }

        self.assertFalse(
            should_do_full_refresh(
                existing_today,
                existing_syncstate,
                "2026-06-22T00:00:00",
            )
        )

    def test_taxon_field_backfill_is_nullable_and_additive(self):
        record = with_taxon_extension_fields(
            {
                "id": "partial",
                "taxonSortOrder": 58199,
            }
        )

        self.assertEqual(record["taxonSortOrder"], 58199)

        for field in TAXON_EXTENSION_FIELDS:
            self.assertIn(field, record)

        self.assertIsNone(record["taxonFamily"])
        self.assertIsNone(record["taxonFamilySwedish"])

    def test_taxon_family_swedish_backfill_uses_cached_taxon_family(self):
        record = with_taxon_extension_fields(
            {
                "id": "old-cache",
                "taxonFamily": "Laridae",
            }
        )

        self.assertEqual(record["taxonFamilySwedish"], "Måsar och trutar")

    def test_taxon_family_swedish_lookup_unknown_family_is_none(self):
        self.assertIsNone(taxon_family_swedish_name("Unknownidae"))

    def test_taxon_family_swedish_metadata_is_valid_json(self):
        path = Path(PROJECT_ROOT) / "metadata" / "taxon_family_sv.json"

        with path.open(encoding="utf-8") as file:
            metadata = json.load(file)

        self.assertEqual(metadata["Laridae"], "Måsar och trutar")


if __name__ == "__main__":
    unittest.main()
