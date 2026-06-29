"""
Boutique home-video label metadata.

Platekompaniet exposes a `product_collection` field whose values include the
releasing label (e.g. "The Criterion Collection", "Arrow Films"). We use it to
build the Criterion collector view and to flag UK/other-region alternatives for
the same film.

Regions are the *typical* region the label's discs ship from / are coded for, as
relevant to a Norwegian buyer deciding between a US import and a UK release.
"""

from __future__ import annotations

CRITERION = "The Criterion Collection"

# label name -> region tag ("US", "UK", "AUS"). Only curated boutique labels.
BOUTIQUE_LABELS: dict[str, str] = {
    CRITERION: "US",          # the user imports these from the US
    "Vinegar Syndrome": "US",
    "Severin Films": "US",
    "Kino Lorber": "US",
    "Shout Factory": "US",
    "Criterion Collection": "US",
    # --- UK boutique labels (the alternatives we want to surface) ---
    "Arrow Films": "UK",
    "Arrow Video": "UK",
    "Second Sight": "UK",
    "Second Sight Films": "UK",
    "88 Films": "UK",
    "Powerhouse Films": "UK",
    "Indicator": "UK",
    "Vintage Classics": "UK",
    "Radiance Films": "UK",
    "Radiance": "UK",
    "BFI": "UK",
    "Studiocanal": "UK",
    "Eureka": "UK",
    "Eureka Entertainment": "UK",
    "Masters of Cinema": "UK",
    "Fabulous Films": "UK",
    # --- Other regions ---
    "Imprint Films": "AUS",
    "Imprint": "AUS",
    "Umbrella Entertainment": "AUS",
}

_REGION_FLAG = {"UK": "🇬🇧", "US": "🇺🇸", "AUS": "🇦🇺", "EU": "🇪🇺"}

# Criterion's official US barcode prefix (UPC company prefix) — identifies the
# canonical US Criterion 4K spine reliably, regardless of how it's tagged.
CRITERION_US_PREFIX = ("715515", "0715515")


def region_from_ean(ean: str | None) -> str | None:
    """
    Infer the release region from a barcode's GS1 prefix. Good enough to tell a
    US import from a UK edition — the distinction a Norwegian buyer cares about.
    """
    if not ean:
        return None
    e = str(ean).strip()
    if e.startswith(CRITERION_US_PREFIX):
        return "US"
    if e[:1] in ("0", "1"):            # UPC, US/Canada
        return "US"
    if e[:2] == "50":                  # 500-509 = UK
        return "UK"
    if e[:2] in ("30", "31", "32", "33", "34", "35", "36", "37",  # France
                 "40", "41", "42", "43", "44",                      # Germany
                 "54", "57", "73", "76"):                           # BE, DK, SE, CH
        return "EU"
    return None


def is_us_criterion_ean(ean: str | None) -> bool:
    return bool(ean) and str(ean).strip().startswith(CRITERION_US_PREFIX)


def boutique_labels_in(labels: list[str]) -> list[str]:
    """Return the curated boutique labels present in a product's label list."""
    return [lbl for lbl in labels if lbl in BOUTIQUE_LABELS]


def region_of(label: str) -> str | None:
    return BOUTIQUE_LABELS.get(label)


def region_flag(region: str | None) -> str:
    return _REGION_FLAG.get(region or "", "")


def is_criterion(labels: list[str]) -> bool:
    return CRITERION in labels
