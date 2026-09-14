from domain.models.part_requirement import Axle
from domain.ontology.parser import find_axle, find_part_type, parse_part_requirement


def test_part_type_synonyms():
    assert find_part_type("jarrupalat") == "brake_pad"
    assert find_part_type("palat") == "brake_pad"
    assert find_part_type("jarrulevyt") == "brake_disc"


def test_axle_synonyms():
    assert find_axle("etu") == Axle.FRONT
    assert find_axle("takana") == Axle.REAR
    assert find_axle("molemmissa") == Axle.BOTH


def test_unknown_terms_are_not_guessed():
    assert find_part_type("jarrukengät") is None
    assert find_axle("jossain päin") == Axle.UNKNOWN


def test_parse_front_brake_pads():
    result = parse_part_requirement("Etujarrupalat")

    assert result is not None
    assert result.canonical_product_type == "brake_pad"
    assert result.axle == Axle.FRONT
    assert result.required_quantity == 1
    assert result.required_unit == "SET"


def test_parse_rear_brake_discs():
    result = parse_part_requirement("takajarrulevyt")

    assert result is not None
    assert result.canonical_product_type == "brake_disc"
    assert result.axle == Axle.REAR


def test_unknown_part_is_not_guessed():
    result = parse_part_requirement("jarrukengät")

    assert result is None
