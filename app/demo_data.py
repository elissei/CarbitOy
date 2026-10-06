from decimal import Decimal

from domain.models.enums import Axle
from domain.models.indexed_product import IndexedProduct, Unit
from domain.models.labour_time import LabourTime, LabourTimeSource
from domain.models.supplier_offer import Availability, SupplierOffer
from domain.models.vehicle import Vehicle


DEMO_REGISTRATION = "ABC-123"


def get_demo_vehicle() -> Vehicle:
    return Vehicle(
        registration=DEMO_REGISTRATION,
        make="Toyota",
        model="Corolla",
        year=2020,
    )


def get_demo_labour_time() -> LabourTime:
    # Simuloitu Autodata-vastaus, ei oikea integraatiokutsu.
    return LabourTime(
        hours=Decimal("0.8"),
        source=LabourTimeSource.AUTODATA,
    )


def get_demo_products() -> list[IndexedProduct]:
    return [
        IndexedProduct(
            product_id="PAD-1",
            supplier="SupplierA",
            supplier_product_id="A-123",
            canonical_product_type="brake_pad",
            axle=Axle.FRONT,
            package_quantity=1,
            package_unit=Unit.SET,
            commercial_quantity=1,
            commercial_unit=Unit.SET,
        ),
        IndexedProduct(
            product_id="PAD-2",
            supplier="SupplierB",
            supplier_product_id="B-456",
            canonical_product_type="brake_pad",
            axle=Axle.FRONT,
            package_quantity=1,
            package_unit=Unit.SET,
            commercial_quantity=1,
            commercial_unit=Unit.SET,
        ),
    ]


def get_demo_offers() -> list[SupplierOffer]:
    return [
        SupplierOffer(
            product_id="PAD-1",
            supplier="SupplierA",
            supplier_product_id="A-123",
            unit_price=Decimal("40.00"),
            currency="EUR",
            availability=Availability.IN_STOCK,
        ),
        SupplierOffer(
            product_id="PAD-2",
            supplier="SupplierB",
            supplier_product_id="B-456",
            unit_price=Decimal("50.00"),
            currency="EUR",
            availability=Availability.IN_STOCK,
        ),
    ]
