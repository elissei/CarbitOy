from domain.models.indexed_product import IndexedProduct, Unit


def test_brake_pad_set_package_and_accounting_quantity():
    product = IndexedProduct(
        product_id="MOTONET-123456",
        supplier="MOTONET",
        supplier_product_id="123456",
        canonical_product_type="brake_pad",
        package_quantity=2,
        package_unit=Unit.PCS,
        accounting_quantity=1,
        accounting_unit=Unit.SET,
    )

    assert product.package_quantity == 2
    assert product.package_unit == Unit.PCS

    assert product.accounting_quantity == 1
    assert product.accounting_unit == Unit.SET
