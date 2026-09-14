from domain.models.vehicle import Vehicle


def test_vehicle():
    vehicle = Vehicle(
        registration="ABC-123",
        make="Volkswagen",
        model="Golf",
        year=2020,
    )

    assert vehicle.registration == "ABC-123"
    assert vehicle.make == "Volkswagen"
    assert vehicle.model == "Golf"
    assert vehicle.year == 2020
