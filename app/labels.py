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

_REGION_FLAG = {"UK": "🇬🇧", "US": "🇺🇸", "AUS": "🇦🇺"}


def boutique_labels_in(labels: list[str]) -> list[str]:
    """Return the curated boutique labels present in a product's label list."""
    return [lbl for lbl in labels if lbl in BOUTIQUE_LABELS]


def region_of(label: str) -> str | None:
    return BOUTIQUE_LABELS.get(label)


def region_flag(region: str | None) -> str:
    return _REGION_FLAG.get(region or "", "")


def is_criterion(labels: list[str]) -> bool:
    return CRITERION in labels
