"""Tests for all Adam normalizers.

Validates: provider response shapes map correctly to canonical records.
Each normalizer test uses a real representative payload from that provider.
No external API calls — all input data is inline.
"""

from __future__ import annotations

import pytest

from aspire_orchestrator.services.adam.normalizers.business_normalizer import (
    normalize_from_foursquare,
    normalize_from_google_places,
    normalize_from_here,
    normalize_phone,
)
from aspire_orchestrator.services.adam.normalizers.hotel_normalizer import (
    normalize_from_tripadvisor,
)
from aspire_orchestrator.services.adam.normalizers.product_normalizer import (
    normalize_from_serpapi_homedepot,
    normalize_from_serpapi_shopping,
)
from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
    normalize_from_attom_detail,
)
from aspire_orchestrator.services.adam.normalizers.web_normalizer import (
    normalize_from_brave,
    normalize_from_exa,
    normalize_from_parallel,
)
from aspire_orchestrator.services.adam.schemas.business_record import BusinessRecord
from aspire_orchestrator.services.adam.schemas.hotel_record import HotelRecord
from aspire_orchestrator.services.adam.schemas.product_record import ProductRecord
from aspire_orchestrator.services.adam.schemas.property_record import PropertyRecord
from aspire_orchestrator.services.adam.schemas.web_evidence import WebEvidence


# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------


class TestNormalizePhone:
    """normalize_phone converts raw strings to (XXX) XXX-XXXX."""

    def test_10_digit_no_formatting(self):
        assert normalize_phone("8005551234") == "(800) 555-1234"

    def test_11_digit_with_country_code_1(self):
        assert normalize_phone("18005551234") == "(800) 555-1234"

    def test_formatted_input_cleaned_and_reformatted(self):
        assert normalize_phone("(800) 555-1234") == "(800) 555-1234"

    def test_dashes_and_dots_stripped(self):
        assert normalize_phone("800-555-1234") == "(800) 555-1234"

    def test_empty_string_returns_empty(self):
        assert normalize_phone("") == ""

    def test_international_non_us_returned_as_is(self):
        """Non-US numbers (not 10 or 11 digits) are returned stripped."""
        result = normalize_phone("+44 20 7946 0958")
        assert isinstance(result, str)
        assert result != ""

    def test_7_digit_number_returned_as_is(self):
        """Short numbers that don't match 10/11 digit pattern return stripped raw."""
        result = normalize_phone("5551234")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Google Places normalizer
# ---------------------------------------------------------------------------


class TestNormalizeFromGooglePlaces:
    """normalize_from_google_places maps Google Places (New) API fields."""

    def _payload(self) -> dict:
        return {
            "displayName": {"text": "Acme Roofing LLC"},
            "formattedAddress": "123 Main St, Lexington, KY 40509",
            "nationalPhoneNumber": "8595551234",
            "websiteUri": "https://acmeroofing.com",
            "primaryTypeDisplayName": {"text": "Roofing contractor"},
            "rating": 4.7,
            "userRatingCount": 82,
            "location": {"latitude": 38.03, "longitude": -84.50},
        }

    def test_name_mapped_from_display_name(self):
        record = normalize_from_google_places(self._payload())
        assert record.name == "Acme Roofing LLC"

    def test_address_mapped(self):
        record = normalize_from_google_places(self._payload())
        assert record.normalized_address == "123 Main St, Lexington, KY 40509"

    def test_phone_normalized(self):
        record = normalize_from_google_places(self._payload())
        assert record.phone == "(859) 555-1234"

    def test_rating_mapped(self):
        record = normalize_from_google_places(self._payload())
        assert record.rating == 4.7

    def test_review_count_mapped(self):
        record = normalize_from_google_places(self._payload())
        assert record.review_count == 82

    def test_source_attribution_is_google_places(self):
        record = normalize_from_google_places(self._payload())
        assert len(record.sources) == 1
        assert record.sources[0].provider == "google_places"

    def test_returns_business_record(self):
        record = normalize_from_google_places(self._payload())
        assert isinstance(record, BusinessRecord)

    def test_empty_payload_does_not_raise(self):
        record = normalize_from_google_places({})
        assert isinstance(record, BusinessRecord)
        assert record.name == ""


# ---------------------------------------------------------------------------
# HERE normalizer
# ---------------------------------------------------------------------------


class TestNormalizeFromHere:
    """normalize_from_here maps HERE Geocoding & Search fields."""

    def _payload(self) -> dict:
        return {
            "title": "Beta HVAC Services",
            "address": {"label": "456 Oak Ave, Louisville, KY 40202"},
            "position": {"lat": 38.25, "lng": -85.75},
            "categories": [{"name": "HVAC Contractor"}, {"name": "Heating"}],
            "contacts": [
                {
                    "phone": [{"value": "5025559876"}],
                    "www": [{"value": "https://betahvac.com"}],
                }
            ],
        }

    def test_name_mapped_from_title(self):
        record = normalize_from_here(self._payload())
        assert record.name == "Beta HVAC Services"

    def test_address_mapped_from_address_label(self):
        record = normalize_from_here(self._payload())
        assert record.normalized_address == "456 Oak Ave, Louisville, KY 40202"

    def test_phone_extracted_from_contacts(self):
        record = normalize_from_here(self._payload())
        assert record.phone == "(502) 555-9876"

    def test_website_extracted_from_contacts(self):
        record = normalize_from_here(self._payload())
        assert record.website == "https://betahvac.com"

    def test_category_built_from_categories(self):
        record = normalize_from_here(self._payload())
        assert "HVAC Contractor" in record.category

    def test_source_attribution_is_here(self):
        record = normalize_from_here(self._payload())
        assert record.sources[0].provider == "here"

    def test_empty_contacts_does_not_raise(self):
        payload = self._payload()
        payload["contacts"] = []
        record = normalize_from_here(payload)
        assert record.phone == ""


# ---------------------------------------------------------------------------
# Foursquare normalizer
# ---------------------------------------------------------------------------


class TestNormalizeFromFoursquare:
    """normalize_from_foursquare maps Foursquare Places API fields."""

    def _payload(self) -> dict:
        return {
            "name": "Gamma Plumbing",
            "location": {
                "formatted_address": "789 Elm St, Cincinnati, OH 45202"
            },
            "geocodes": {
                "main": {"latitude": 39.1, "longitude": -84.5}
            },
            "categories": [{"name": "Plumbing Service"}],
            "tel": "5135557890",
            "website": "https://gammaplumbing.com",
            "rating": 8.2,
            "distance": 3218,  # meters (~2 miles)
        }

    def test_name_mapped(self):
        record = normalize_from_foursquare(self._payload())
        assert record.name == "Gamma Plumbing"

    def test_address_mapped(self):
        record = normalize_from_foursquare(self._payload())
        assert record.normalized_address == "789 Elm St, Cincinnati, OH 45202"

    def test_phone_normalized(self):
        record = normalize_from_foursquare(self._payload())
        assert record.phone == "(513) 555-7890"

    def test_geocodes_latitude_longitude(self):
        record = normalize_from_foursquare(self._payload())
        assert record.latitude == 39.1
        assert record.longitude == -84.5

    def test_distance_converted_to_miles(self):
        """3218 meters → approximately 2.0 miles."""
        record = normalize_from_foursquare(self._payload())
        assert record.distance_miles is not None
        assert abs(record.distance_miles - 2.0) < 0.2

    def test_category_from_categories_list(self):
        record = normalize_from_foursquare(self._payload())
        assert record.category == "Plumbing Service"

    def test_source_attribution_is_foursquare(self):
        record = normalize_from_foursquare(self._payload())
        assert record.sources[0].provider == "foursquare"


# ---------------------------------------------------------------------------
# ATTOM property normalizer
# ---------------------------------------------------------------------------


class TestNormalizeFromAttomDetail:
    """normalize_from_attom_detail maps ATTOM property/detail response."""

    def _payload(self) -> dict:
        return {
            "property": [
                {
                    "identifier": {"apn": "123-456-789", "fips": "21067", "attomId": "999001"},
                    "address": {"oneLine": "123 Main St, Lexington, KY 40509"},
                    "building": {
                        "summary": {
                            "proptype": "SFR",
                            "yearbuilt": 1998,
                            "livingsize": 2100,
                            "beds": 3,
                        },
                        "rooms": {"bathstotal": 2.5, "beds": 3},
                        "size": {"livingsize": 2100},
                        "construction": {"frameType": "Wood"},
                        "roof": {"cover": "Shingle"},
                    },
                    "lot": {"lotsize2": 8500},
                    "assessment": {
                        "assessed": {"assdTtlValue": 185000}
                    },
                    "owner": {
                        "owner1": {"fullName": "John Q. Homeowner"},
                        "corporateIndicator": "N",
                        "absenteeOwnerStatus": "N",
                    },
                    "sale": {
                        "saleTransDate": "2019-06-15",
                        "saleAmountData": {"saleAmt": 245000},
                    },
                    "vintage": {"lastModified": "2024-11-01"},
                }
            ]
        }

    def test_address_normalized(self):
        record = normalize_from_attom_detail(self._payload())
        assert "123 Main St" in record.normalized_address

    def test_year_built(self):
        record = normalize_from_attom_detail(self._payload())
        assert record.year_built == 1998

    def test_living_sqft(self):
        record = normalize_from_attom_detail(self._payload())
        assert record.living_sqft == 2100

    def test_beds(self):
        record = normalize_from_attom_detail(self._payload())
        assert record.beds == 3

    def test_baths(self):
        record = normalize_from_attom_detail(self._payload())
        assert record.baths == 2.5

    def test_lot_sqft(self):
        record = normalize_from_attom_detail(self._payload())
        assert record.lot_sqft == 8500

    def test_owner_name(self):
        record = normalize_from_attom_detail(self._payload())
        assert record.owner_name == "John Q. Homeowner"

    def test_last_sale_date(self):
        record = normalize_from_attom_detail(self._payload())
        assert record.last_sale_date == "2019-06-15"

    def test_last_sale_amount(self):
        record = normalize_from_attom_detail(self._payload())
        assert record.last_sale_amount == 245000.0

    def test_verification_status_verified(self):
        record = normalize_from_attom_detail(self._payload())
        assert record.verification_status == "verified"

    def test_source_attribution_is_attom(self):
        record = normalize_from_attom_detail(self._payload())
        assert record.sources[0].provider == "attom"

    def test_returns_property_record(self):
        record = normalize_from_attom_detail(self._payload())
        assert isinstance(record, PropertyRecord)

    def test_empty_property_list_returns_unverified(self):
        """Empty property array → unverified PropertyRecord (fail-closed, never invent facts)."""
        record = normalize_from_attom_detail({"property": []})
        assert record.verification_status == "unverified"
        assert record.normalized_address == ""


# ---------------------------------------------------------------------------
# SerpApi Shopping normalizer
# ---------------------------------------------------------------------------


class TestNormalizeFromSerpApiShopping:
    """normalize_from_serpapi_shopping maps Google Shopping result fields."""

    def _payload(self) -> dict:
        return {
            "title": "Goodman 3 Ton 14 SEER R-410A Central Air Conditioner",
            "extracted_price": 1149.00,
            "source": "HVAC Direct",          # source = retailer (NOT brand)
            "product_link": "https://hvacdirect.com/goodman-3ton",
            "thumbnail": "https://img.example.com/goodman.jpg",
            "rating": 4.5,
            "reviews": 312,
            "delivery": "Free shipping",
            "extensions": ["Goodman", "Free shipping"],  # first non-delivery extension = brand
        }

    def test_product_name_mapped(self):
        record = normalize_from_serpapi_shopping(self._payload())
        assert "3 Ton" in record.product_name

    def test_price_mapped_from_extracted_price(self):
        record = normalize_from_serpapi_shopping(self._payload())
        assert record.price == 1149.00

    def test_retailer_mapped_from_source_not_brand(self):
        """'source' field = retailer. ADR-003: never conflate source with brand."""
        record = normalize_from_serpapi_shopping(self._payload())
        assert record.retailer == "HVAC Direct"

    def test_rating_mapped(self):
        record = normalize_from_serpapi_shopping(self._payload())
        assert record.rating == 4.5

    def test_reviews_mapped(self):
        record = normalize_from_serpapi_shopping(self._payload())
        assert record.reviews == 312

    def test_currency_is_usd(self):
        record = normalize_from_serpapi_shopping(self._payload())
        assert record.currency == "USD"

    def test_source_attribution_is_serpapi_shopping(self):
        record = normalize_from_serpapi_shopping(self._payload())
        assert record.sources[0].provider == "serpapi_shopping"

    def test_returns_product_record(self):
        record = normalize_from_serpapi_shopping(self._payload())
        assert isinstance(record, ProductRecord)


# ---------------------------------------------------------------------------
# SerpApi Home Depot normalizer
# ---------------------------------------------------------------------------


class TestNormalizeFromSerpApiHomeDepot:
    """normalize_from_serpapi_homedepot maps Home Depot result fields."""

    def _payload(self) -> dict:
        return {
            "title": "Rheem 3 Ton 16 SEER Condenser",
            "brand": "Rheem",
            "model_number": "RA1636AJ1NA",
            "product_id": 205678901,
            "price": 1325.00,
            "price_was": 1499.00,
            "price_saving": 174.00,
            "percentage_off": 11.6,
            "pickup": {"quantity": 4, "store_name": "Louisville East"},
            "delivery": {"free": "Free delivery on orders over $45"},
            "link": "https://homedepot.com/p/rheem/205678901",
            "thumbnails": [["https://img.homedepot.com/rheem.jpg"]],
            "rating": 4.2,
            "reviews": 156,
        }

    def test_product_name_mapped(self):
        record = normalize_from_serpapi_homedepot(self._payload())
        assert "Rheem" in record.product_name or "Condenser" in record.product_name

    def test_brand_mapped(self):
        record = normalize_from_serpapi_homedepot(self._payload())
        assert record.brand == "Rheem"

    def test_model_number_mapped(self):
        record = normalize_from_serpapi_homedepot(self._payload())
        assert record.model == "RA1636AJ1NA"

    def test_sku_from_product_id(self):
        record = normalize_from_serpapi_homedepot(self._payload())
        assert record.sku == "205678901"

    def test_retailer_is_home_depot(self):
        record = normalize_from_serpapi_homedepot(self._payload())
        assert record.retailer == "Home Depot"

    def test_price_mapped(self):
        record = normalize_from_serpapi_homedepot(self._payload())
        assert record.price == 1325.00

    def test_price_was_mapped(self):
        record = normalize_from_serpapi_homedepot(self._payload())
        assert record.price_was == 1499.00

    def test_in_store_stock_from_pickup_quantity(self):
        record = normalize_from_serpapi_homedepot(self._payload())
        assert record.in_store_stock == 4

    def test_availability_in_stock_when_quantity_positive(self):
        record = normalize_from_serpapi_homedepot(self._payload())
        assert record.availability == "in_stock"

    def test_availability_check_store_when_quantity_zero(self):
        payload = self._payload()
        payload["pickup"]["quantity"] = 0
        record = normalize_from_serpapi_homedepot(payload)
        assert record.availability == "check_store"

    def test_image_url_extracted_from_nested_thumbnails(self):
        record = normalize_from_serpapi_homedepot(self._payload())
        assert "homedepot.com" in record.image_url or "img" in record.image_url

    def test_image_url_extracted_from_dict_thumbnail_entries(self):
        payload = self._payload()
        payload["thumbnail"] = ""
        payload["thumbnails"] = [{"url": "https://img.homedepot.com/rheem-dict.jpg"}]
        record = normalize_from_serpapi_homedepot(payload)
        assert record.image_url == "https://img.homedepot.com/rheem-dict.jpg"

    def test_explicit_thumbnail_string_preferred_over_invalid_thumbnails(self):
        payload = self._payload()
        payload["thumbnail"] = "https://img.homedepot.com/rheem-explicit.jpg"
        payload["thumbnails"] = [{"bad": "value"}]
        record = normalize_from_serpapi_homedepot(payload)
        assert record.image_url == "https://img.homedepot.com/rheem-explicit.jpg"

    def test_source_attribution_is_serpapi_home_depot(self):
        record = normalize_from_serpapi_homedepot(self._payload())
        assert record.sources[0].provider == "serpapi_home_depot"


# ---------------------------------------------------------------------------
# Tripadvisor hotel normalizer
# ---------------------------------------------------------------------------


class TestNormalizeFromTripAdvisor:
    """normalize_from_tripadvisor maps Tripadvisor location search fields."""

    def _payload(self) -> dict:
        return {
            "name": "The Convention Center Marriott",
            "address_obj": {
                "street1": "100 Convention Way",
                "city": "Nashville",
                "state": "TN",
                "postalcode": "37203",
            },
            "rating": "4.5",
            "num_reviews": "1842",
            "hotel_class": "4.0",
            "price_level": "$$$",
            "latitude": "36.16",
            "longitude": "-86.78",
            "ranking_data": {"ranking_string": "#3 of 250 hotels in Nashville"},
            "subcategory": [{"name": "hotel"}, {"name": "business center"}],
        }

    def test_name_mapped(self):
        record = normalize_from_tripadvisor(self._payload())
        assert record.name == "The Convention Center Marriott"

    def test_address_assembled_from_address_obj(self):
        record = normalize_from_tripadvisor(self._payload())
        assert "100 Convention Way" in record.normalized_address
        assert "Nashville" in record.normalized_address

    def test_rating_mapped_from_rating_field(self):
        record = normalize_from_tripadvisor(self._payload())
        assert record.traveler_rating == 4.5

    def test_review_count_mapped(self):
        record = normalize_from_tripadvisor(self._payload())
        assert record.review_count == 1842

    def test_star_rating_from_hotel_class(self):
        record = normalize_from_tripadvisor(self._payload())
        assert record.star_rating == 4.0

    def test_price_level_mapped(self):
        record = normalize_from_tripadvisor(self._payload())
        assert record.price_range == "$$$"

    def test_sentiment_summary_from_ranking_data(self):
        record = normalize_from_tripadvisor(self._payload())
        assert "#3 of 250" in record.sentiment_summary

    def test_ranking_string_exposed_for_card_renderers(self):
        record = normalize_from_tripadvisor(self._payload())
        assert record.extra.get("ranking_string") == "#3 of 250 hotels in Nashville"

    def test_source_attribution_is_tripadvisor(self):
        record = normalize_from_tripadvisor(self._payload())
        assert record.sources[0].provider == "tripadvisor"

    def test_returns_hotel_record(self):
        record = normalize_from_tripadvisor(self._payload())
        assert isinstance(record, HotelRecord)


# ---------------------------------------------------------------------------
# Exa web normalizer
# ---------------------------------------------------------------------------


class TestNormalizeFromExa:
    """normalize_from_exa maps Exa result with optional grounding data."""

    def _payload(self) -> dict:
        return {
            "url": "https://irs.gov/quarterly-taxes",
            "title": "Estimated Tax | Internal Revenue Service",
            "text": "For 2025, the estimated tax payment deadlines are April 15...",
            "summary": "IRS official page on quarterly estimated taxes",
            "highlights": ["April 15", "June 15", "September 15", "January 15"],
            "publishedDate": "2024-12-01",
        }

    def test_url_mapped(self):
        record = normalize_from_exa(self._payload())
        assert record.url == "https://irs.gov/quarterly-taxes"

    def test_title_mapped(self):
        record = normalize_from_exa(self._payload())
        assert "Internal Revenue" in record.title

    def test_content_from_text_field(self):
        record = normalize_from_exa(self._payload())
        assert "estimated tax" in record.content.lower()

    def test_summary_mapped(self):
        record = normalize_from_exa(self._payload())
        assert "quarterly" in record.summary

    def test_highlights_mapped(self):
        record = normalize_from_exa(self._payload())
        assert "April 15" in record.highlights

    def test_domain_extracted_from_url(self):
        record = normalize_from_exa(self._payload())
        assert record.domain == "irs.gov"

    def test_grounding_high_maps_confidence_0_90(self):
        grounding = {"confidence": "high", "source": "irs.gov"}
        record = normalize_from_exa(self._payload(), grounding=grounding)
        assert record.confidence == 0.90
        assert record.exa_grounding_confidence == "high"

    def test_grounding_medium_maps_confidence_0_70(self):
        grounding = {"confidence": "medium"}
        record = normalize_from_exa(self._payload(), grounding=grounding)
        assert record.confidence == 0.70
        assert record.exa_grounding_confidence == "medium"

    def test_grounding_low_maps_confidence_0_40(self):
        grounding = {"confidence": "low"}
        record = normalize_from_exa(self._payload(), grounding=grounding)
        assert record.confidence == 0.40

    def test_no_grounding_confidence_is_zero(self):
        record = normalize_from_exa(self._payload(), grounding=None)
        assert record.confidence == 0.0
        assert record.exa_grounding_confidence == ""

    def test_provider_is_exa(self):
        record = normalize_from_exa(self._payload())
        assert record.provider == "exa"

    def test_returns_web_evidence(self):
        record = normalize_from_exa(self._payload())
        assert isinstance(record, WebEvidence)


# ---------------------------------------------------------------------------
# Brave web normalizer
# ---------------------------------------------------------------------------


class TestNormalizeFromBrave:
    """normalize_from_brave maps Brave Search result fields."""

    def _payload(self) -> dict:
        return {
            "url": "https://constructiondive.com/roofing-material-prices",
            "title": "2025 Roofing Material Price Guide",
            "description": "Current prices for shingles, tiles, and metal roofing...",
            "age": "2025-03-01",
        }

    def test_url_mapped(self):
        record = normalize_from_brave(self._payload())
        assert record.url == "https://constructiondive.com/roofing-material-prices"

    def test_title_mapped(self):
        record = normalize_from_brave(self._payload())
        assert "Roofing" in record.title

    def test_snippet_from_description(self):
        record = normalize_from_brave(self._payload())
        assert "prices" in record.snippet.lower()

    def test_domain_extracted(self):
        record = normalize_from_brave(self._payload())
        assert record.domain == "constructiondive.com"

    def test_published_date_from_age(self):
        record = normalize_from_brave(self._payload())
        assert record.published_date == "2025-03-01"

    def test_provider_is_brave(self):
        record = normalize_from_brave(self._payload())
        assert record.provider == "brave"

    def test_returns_web_evidence(self):
        record = normalize_from_brave(self._payload())
        assert isinstance(record, WebEvidence)


# ---------------------------------------------------------------------------
# Parallel web normalizer
# ---------------------------------------------------------------------------


class TestNormalizeFromParallel:
    """normalize_from_parallel maps Parallel search result fields."""

    def _payload(self) -> dict:
        return {
            "url": "https://nolo.com/tenant-screening-rules",
            "title": "Tenant Screening Laws by State",
            "excerpt": "Fair housing laws prohibit discrimination based on...",
            "source_domain": "nolo.com",
            "published_date": "2024-10-15",
        }

    def test_url_mapped(self):
        record = normalize_from_parallel(self._payload())
        assert record.url == "https://nolo.com/tenant-screening-rules"

    def test_title_mapped(self):
        record = normalize_from_parallel(self._payload())
        assert "Tenant Screening" in record.title

    def test_snippet_from_excerpt(self):
        record = normalize_from_parallel(self._payload())
        assert "fair housing" in record.snippet.lower()

    def test_domain_from_source_domain(self):
        record = normalize_from_parallel(self._payload())
        assert record.domain == "nolo.com"

    def test_published_date_mapped(self):
        record = normalize_from_parallel(self._payload())
        assert record.published_date == "2024-10-15"

    def test_provider_is_parallel(self):
        record = normalize_from_parallel(self._payload())
        assert record.provider == "parallel"

    def test_returns_web_evidence(self):
        record = normalize_from_parallel(self._payload())
        assert isinstance(record, WebEvidence)

    def test_domain_falls_back_to_url_netloc(self):
        """When source_domain missing, domain extracted from URL netloc."""
        payload = self._payload()
        del payload["source_domain"]
        record = normalize_from_parallel(payload)
        assert record.domain == "nolo.com"


# ---------------------------------------------------------------------------
# Google Places hotel normalizer (coverage gap fix)
# ---------------------------------------------------------------------------

from aspire_orchestrator.services.adam.normalizers.hotel_normalizer import (
    normalize_from_google_places_hotel,
    normalize_from_here_hotel,
)


class TestNormalizeFromGooglePlacesHotel:
    """normalize_from_google_places_hotel maps GP result to HotelRecord."""

    def _payload(self) -> dict:
        return {
            "displayName": {"text": "Hilton Nashville Downtown"},
            "formattedAddress": "121 4th Ave S, Nashville, TN 37201",
            "rating": 4.3,
            "userRatingCount": 2850,
            "priceLevel": "PRICE_LEVEL_EXPENSIVE",
            "location": {"latitude": 36.1585, "longitude": -86.7768},
        }

    def test_name_mapped(self):
        record = normalize_from_google_places_hotel(self._payload())
        assert record.name == "Hilton Nashville Downtown"

    def test_address_mapped(self):
        record = normalize_from_google_places_hotel(self._payload())
        assert "121 4th Ave" in record.normalized_address

    def test_rating_mapped(self):
        record = normalize_from_google_places_hotel(self._payload())
        assert record.traveler_rating == 4.3

    def test_review_count_mapped(self):
        record = normalize_from_google_places_hotel(self._payload())
        assert record.review_count == 2850

    def test_price_level_mapped_to_dollars(self):
        record = normalize_from_google_places_hotel(self._payload())
        assert record.price_range == "$$$"

    def test_price_level_moderate(self):
        payload = self._payload()
        payload["priceLevel"] = "PRICE_LEVEL_MODERATE"
        record = normalize_from_google_places_hotel(payload)
        assert record.price_range == "$$"

    def test_location_mapped(self):
        record = normalize_from_google_places_hotel(self._payload())
        assert record.latitude == 36.1585
        assert record.longitude == -86.7768

    def test_source_is_google_places(self):
        record = normalize_from_google_places_hotel(self._payload())
        assert record.sources[0].provider == "google_places"

    def test_returns_hotel_record(self):
        record = normalize_from_google_places_hotel(self._payload())
        assert isinstance(record, HotelRecord)


class TestNormalizeFromHereHotel:
    """normalize_from_here_hotel maps HERE result to HotelRecord."""

    def _payload(self) -> dict:
        return {
            "title": "Omni Nashville Hotel",
            "address": {"label": "250 Rep. John Lewis Way S, Nashville, TN 37203"},
            "position": {"lat": 36.1565, "lng": -86.7760},
            "contacts": [{"www": [{"value": "https://omnihotels.com"}]}],
        }

    def test_name_mapped(self):
        record = normalize_from_here_hotel(self._payload())
        assert record.name == "Omni Nashville Hotel"

    def test_address_mapped(self):
        record = normalize_from_here_hotel(self._payload())
        assert "250 Rep. John Lewis" in record.normalized_address

    def test_location_mapped(self):
        record = normalize_from_here_hotel(self._payload())
        assert record.latitude == 36.1565
        assert record.longitude == -86.7760

    def test_website_in_extra(self):
        record = normalize_from_here_hotel(self._payload())
        assert record.extra.get("website") == "https://omnihotels.com"

    def test_source_is_here(self):
        record = normalize_from_here_hotel(self._payload())
        assert record.sources[0].provider == "here"

    def test_no_website_no_extra(self):
        payload = self._payload()
        payload["contacts"] = []
        record = normalize_from_here_hotel(payload)
        assert record.extra == {}


# ---------------------------------------------------------------------------
# Property normalizer — sales history, valuation, rental (coverage gap fix)
# ---------------------------------------------------------------------------

from aspire_orchestrator.services.adam.normalizers.property_normalizer import (
    normalize_from_attom_sales_history,
    normalize_from_attom_valuation,
    normalize_from_attom_rental,
)
from aspire_orchestrator.services.adam.schemas.property_record import SaleRecord


class TestNormalizeAttomSalesHistory:
    """normalize_from_attom_sales_history returns list[SaleRecord]."""

    def _payload(self) -> dict:
        return {
            "property": [{
                "saleHistory": [
                    {
                        "amount": {"saleRecDate": "2023-06-15", "saleAmt": 350000, "saleTransType": "Resale"},
                        "buyer1FullName": "John Smith",
                        "seller1FullName": "Jane Doe",
                    },
                    {
                        "amount": {"saleRecDate": "2019-03-01", "saleAmt": 280000, "saleTransType": "Resale"},
                        "buyer1FullName": "Jane Doe",
                        "seller1FullName": "Bob Builder",
                    },
                ]
            }]
        }

    def test_returns_list_of_sale_records(self):
        sales = normalize_from_attom_sales_history(self._payload())
        assert len(sales) == 2
        assert isinstance(sales[0], SaleRecord)

    def test_sale_date_mapped(self):
        sales = normalize_from_attom_sales_history(self._payload())
        assert sales[0].date == "2023-06-15"

    def test_sale_amount_mapped(self):
        sales = normalize_from_attom_sales_history(self._payload())
        assert sales[0].amount == 350000.0

    def test_buyer_seller_mapped(self):
        sales = normalize_from_attom_sales_history(self._payload())
        assert sales[0].buyer == "John Smith"
        assert sales[0].seller == "Jane Doe"

    def test_trans_type_mapped(self):
        sales = normalize_from_attom_sales_history(self._payload())
        assert sales[0].trans_type == "Resale"

    def test_empty_property_returns_empty_list(self):
        sales = normalize_from_attom_sales_history({"property": []})
        assert sales == []

    def test_no_property_key_returns_empty_list(self):
        sales = normalize_from_attom_sales_history({})
        assert sales == []


class TestNormalizeAttomValuation:
    """normalize_from_attom_valuation returns AVM dict."""

    def _payload(self) -> dict:
        return {
            "property": [{
                "assessment": {
                    "market": {
                        "mktTtlValue": 425000,
                        "mktTtlValueHigh": 460000,
                        "mktTtlValueLow": 390000,
                        "confidence": "high",
                    }
                }
            }]
        }

    def test_estimated_value_mapped(self):
        result = normalize_from_attom_valuation(self._payload())
        assert result["estimated_value"] == 425000.0

    def test_value_high_mapped(self):
        result = normalize_from_attom_valuation(self._payload())
        assert result["estimated_value_high"] == 460000.0

    def test_value_low_mapped(self):
        result = normalize_from_attom_valuation(self._payload())
        assert result["estimated_value_low"] == 390000.0

    def test_confidence_mapped(self):
        result = normalize_from_attom_valuation(self._payload())
        assert result["valuation_confidence"] == "high"

    def test_empty_property_returns_empty_dict(self):
        result = normalize_from_attom_valuation({"property": []})
        assert result == {}


class TestNormalizeAttomRental:
    """normalize_from_attom_rental returns rental AVM dict."""

    def _payload(self) -> dict:
        return {
            "property": [{
                "rental": {
                    "rentAmount": 2200,
                    "rentHigh": 2500,
                    "rentLow": 1900,
                }
            }]
        }

    def test_estimated_rent_mapped(self):
        result = normalize_from_attom_rental(self._payload())
        assert result["estimated_rent"] == 2200.0

    def test_rent_high_mapped(self):
        result = normalize_from_attom_rental(self._payload())
        assert result["estimated_rent_high"] == 2500.0

    def test_rent_low_mapped(self):
        result = normalize_from_attom_rental(self._payload())
        assert result["estimated_rent_low"] == 1900.0

    def test_empty_property_returns_empty_dict(self):
        result = normalize_from_attom_rental({"property": []})
        assert result == {}
