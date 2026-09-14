from domain.models.part_requirement import Axle
from domain.ontology.parser import find_axle, find_part_type


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
