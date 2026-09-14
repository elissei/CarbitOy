from pathlib import Path

import yaml

from domain.models.part_requirement import Axle, PartRequirement


ONTOLOGY_DIR = Path(__file__).resolve().parent


def load_yaml(filename: str) -> dict:
    with open(ONTOLOGY_DIR / filename, encoding="utf-8") as file:
        return yaml.safe_load(file)


def normalize_text(value: str) -> str:
    return value.strip().lower()


def find_part_type(term: str) -> str | None:
    data = load_yaml("synonyms.yaml")
    normalized = normalize_text(term)

    for canonical_type, synonyms in data["synonyms"].items():
        for synonym in synonyms:
            if normalized == normalize_text(synonym):
                return canonical_type

    return None


def find_axle(term: str) -> Axle:
    data = load_yaml("locations.yaml")
    normalized = normalize_text(term)

    for axle_name, synonyms in data["axle"].items():
        for synonym in synonyms:
            normalized_synonym = normalize_text(synonym)

            if normalized == normalized_synonym:
                return Axle(axle_name)

            if normalized_synonym in normalized:
                return Axle(axle_name)

    return Axle.UNKNOWN


def parse_part_requirement(text: str) -> PartRequirement | None:
    normalized = normalize_text(text)

    data = load_yaml("synonyms.yaml")

    canonical_product_type = None

    for canonical_type, synonyms in data["synonyms"].items():
        for synonym in synonyms:
            if normalize_text(synonym) in normalized:
                canonical_product_type = canonical_type
                break
        if canonical_product_type:
            break

    axle = find_axle(normalized)

    if canonical_product_type is None:
        return None

    return PartRequirement(
        canonical_product_type=canonical_product_type,
        axle=axle,
        required_quantity=1,
        required_unit="SET",
    )
