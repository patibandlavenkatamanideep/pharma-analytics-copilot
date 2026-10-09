"""Product classification, and the authority each class comes from.

The supplied data says one thing about a product's class: brand_flag = 1 is
ours. brand_flag = 0 covers branded competitors, generics and biosimilars
alike, so "what is the generic share" cannot be answered from the source.
Any other class comes from a classification MAPPING: a versioned file that
names products by drug name within a market subcategory, as the supplied
documentation lists them (docs/market_classification.md). The supplied
dataset's mapping is classification_supplied.json, curated from that
document; another dataset brings its own (product_classification.json beside
its files, or a path given to the loader) or has none.

Rule 2.0.0, in order:

1. brand_flag = 1                      -> company_brand  (authority: source)
2. an entry in the dataset's mapping   -> its class      (authority: mapping)
3. otherwise                           -> unknown        (authority: none)

Rule 1.0.0 classified by name: a ' GENERIC' or ' BIOSIMILAR' suffix, and
branded_competitor for any other brand_flag 0 product. The suffix is a
convention of the supplied generator, not of product data, and the last step
classed by elimination: a generic sold under a trade name -- or anything at
all -- became a branded competitor, and segment shares counted it as one
(qualification of 7 October 2026, step 4). An unknown product is counted in
its market and in no segment; a segment share carries its volume as a range
(app/analytics/compiler.py, market_segment_share).

A mapping that contradicts the source is refused -- company_brand for a
brand_flag 0 product, another class for a brand_flag 1 product -- as are a
key listed twice and a class outside the four. Leaving a product out is how a
mapping says its class is unknown.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass
from typing import NamedTuple

RULE_VERSION = "2.0.0"
CLASSES = ("company_brand", "branded_competitor", "generic", "biosimilar")
SUPPLIED_MAPPING = pathlib.Path(__file__).with_name("classification_supplied.json")
#: The mapping a dataset directory carries beside its CSV files.
MAPPING_FILE = "product_classification.json"


class MappingError(ValueError):
    """A classification mapping that cannot be used as an authority."""


@dataclass(frozen=True)
class Mapping:
    name: str
    version: str
    sha256: str
    authority: str
    #: (DRUG NAME upper-cased, market_subcategory) -> class
    entries: dict[tuple[str, str], str]

    @property
    def ref(self) -> str:
        return f"{self.name} {self.version} (sha256 {self.sha256[:12]})"


class Classified(NamedTuple):
    classification: str
    derivation: str
    authority: str          # source | mapping | none


def read_mapping(path: pathlib.Path) -> Mapping:
    raw = path.read_bytes()
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise MappingError(f"{path.name} is not JSON: {exc}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        raise MappingError(f"{path.name}: expected an object with an 'entries' list")
    for field in ("mapping", "version", "authority"):
        if not isinstance(doc.get(field), str) or not doc[field].strip():
            raise MappingError(f"{path.name}: '{field}' is required")
    entries: dict[tuple[str, str], str] = {}
    for i, entry in enumerate(doc["entries"]):
        if not isinstance(entry, dict):
            raise MappingError(f"{path.name} entry {i}: not an object")
        name = str(entry.get("drug_name") or "").strip().upper()
        subcategory = str(entry.get("market_subcategory") or "").strip()
        cls = entry.get("classification")
        if not name or not subcategory:
            raise MappingError(f"{path.name} entry {i}: drug_name and market_subcategory "
                               "are required")
        if cls not in CLASSES:
            raise MappingError(
                f"{path.name} entry {i}: classification {cls!r} is not one of "
                f"{', '.join(CLASSES)}; leave a product out to make its class unknown")
        if (name, subcategory) in entries:
            raise MappingError(f"{path.name} entry {i}: {name} in {subcategory} is listed twice")
        entries[(name, subcategory)] = cls
    return Mapping(name=doc["mapping"].strip(), version=doc["version"].strip(),
                   sha256=hashlib.sha256(raw).hexdigest(), authority=doc["authority"].strip(),
                   entries=entries)


def classify(drug_name: str, brand_flag: int, market_subcategory: str | None = None,
             mapping: Mapping | None = None) -> Classified:
    """One product's class, its derivation and its authority (rule 2.0.0)."""
    name = (drug_name or "").strip().upper()
    subcategory = (market_subcategory or "").strip()
    mapped = mapping.entries.get((name, subcategory)) if mapping is not None else None
    if brand_flag == 1:
        if mapped is not None and mapped != "company_brand":
            raise MappingError(f"mapping {mapping.ref} classes {name} in {subcategory} as "
                               f"{mapped}, but the source marks it brand_flag = 1")
        return Classified("company_brand", "brand_flag=1", "source")
    if mapped == "company_brand":
        raise MappingError(f"mapping {mapping.ref} classes {name} in {subcategory} as "
                           "company_brand, but the source marks it brand_flag = 0")
    if mapped is not None:
        return Classified(mapped, f"mapping {mapping.ref}", "mapping")
    return Classified("unknown", "brand_flag=0 and no mapping entry", "none")
