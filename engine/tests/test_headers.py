"""Column titles written in many ways map to the same engine fields."""
import pandas as pd
import pytest

from app.headers import resolve
from conftest import units


def fields(headers):
    return [f for f, _ in resolve(headers)]


@pytest.mark.parametrize("title", ["PO", "purchase order", "Purchase Order", "purchase_order", "purchase_order_#",
                                   "PO Number", "PO #", "N° commande", "Bon de commande", "Ordine fornitore",
                                   "PurchaseOrderNo", "Commande", "order", "Order No."])
def test_purchase_order_titles(title):
    assert fields([title, "Line", "SKU", "Qty"])[0] == "po_number"


@pytest.mark.parametrize("title", ["Order", "Commande", "Ordine", "Customer order", "Sales order", "Commande client"])
def test_bare_order_is_customer_order_with_customer_columns(title):
    assert fields([title, "Line", "Customer", "SKU", "Qty", "Ship date"])[0] == "order_number"


@pytest.mark.parametrize("headers,expected", [
    (["Référence article", "Désignation", "Qté", "Fournisseur"], ["sku", "description", "qty_units", "supplier"]),
    (["Codice articolo", "Descrizione", "Quantità", "Fornitore"], ["sku", "description", "qty_units", "supplier"]),
    (["item_code", "Quanity", "Suplier"], ["sku", "qty_units", "supplier"]),                       # typos
    (["DDT", "PO", "Riga", "Qty delivered"], ["dn_number", "po_number", "line_ref", "qty_units"]),
    (["Emplacement", "Article", "Qté"], ["case_id", "sku", "qty_units"]),
    (["PO", "PO date", "SKU"], ["po_number", "po_date", "sku"]),                                   # date != number
])
def test_multilingual_and_typos(headers, expected):
    assert fields(headers) == expected


def test_units_are_converted():
    r = dict(zip(["L", "W", "H", "Wt"], resolve(["Longueur (cm)", "Width m", "Hauteur mm", "Poids (g)"])))
    assert r["L"] == ("length_mm", 10.0) and r["W"] == ("width_mm", 1000.0)
    assert r["H"] == ("height_mm", 1.0) and r["Wt"] == ("weight_kg", 0.001)


def test_french_po_file_with_cm_and_grams_is_ingested(client, tmp_path):
    f = tmp_path / "commandes.xlsx"
    pd.DataFrame([["BC-77", 1, "Freins Lyon", "PLQ-1", "Plaquettes", 2, "colis", 42, 30, 18, 6500]],
                 columns=["N° commande", "Ligne", "Fournisseur", "Référence", "Désignation", "Quantité",
                          "Conditionnement", "Longueur (cm)", "Largeur (cm)", "Hauteur (cm)", "Poids (g)"]
                 ).to_excel(f, index=False)
    r = client.post("/ingest/file", json={"event": "modified", "path": str(f)}).json()
    assert r["kind"] == "purchase_order" and r["inserted"] == 1, r
    u = units(client, "BC-77-1")[0]
    assert (u["length_m"], u["width_m"], u["height_m"], u["weight_kg"]) == (0.44, 0.32, 0.2, 7.0)   # +carton


@pytest.mark.parametrize("word,expected", [("Palette", "pallet"), ("pallets", "pallet"), ("bancale", "pallet"),
                                           ("colis", "parcel"), ("Carton", "parcel"), ("scatola", "parcel"),
                                           ("minuteria", "small"), ("Sachet", "small")])
def test_packaging_words(word, expected):
    from app.ingest import parse_packaging
    assert parse_packaging(word) == expected


@pytest.mark.parametrize("word,expected", [("oui", 1), ("Sì", 1), ("non", 0), ("très fragile", 2), ("", None)])
def test_fragile_words(word, expected):
    from app.ingest import parse_fragility
    assert parse_fragility(word) == expected
