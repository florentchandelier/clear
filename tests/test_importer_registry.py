"""Regression coverage for the public importer registry contract."""

from app.services.importers.loc.bmo_heloc_fr import BmoHelocFrImporter
from app.services.importers.registry import (
    importer_by_key,
    list_importers,
    public_catalog,
)


def _catalog_keys():
    return [
        entry["key"]
        for classes in public_catalog().values()
        for entries in classes.values()
        for entry in entries
    ]


def test_heloc_uses_canonical_loc_key_without_legacy_alias():
    importer = importer_by_key("loc.bmo_heloc_fr")

    assert isinstance(importer, BmoHelocFrImporter)
    assert importer.account_side == "liability"
    assert importer.account_class == "loc"
    assert importer_by_key("loan.bmo_heloc_fr") is None


def test_loc_registry_and_catalog_expose_only_the_canonical_key():
    importers = list_importers("liability", "loc")
    assert [importer.key for importer in importers] == ["loc.bmo_heloc_fr"]

    catalog_entries = public_catalog()["liability"]["loc"]
    assert [entry["key"] for entry in catalog_entries] == ["loc.bmo_heloc_fr"]
    assert "loan.bmo_heloc_fr" not in _catalog_keys()


def test_registered_importer_keys_are_unique():
    keys = _catalog_keys()
    assert len(keys) == len(set(keys))
