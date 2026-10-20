"""Column-title recognition: maps whatever people write in their sheets to the engine's fields.

Tolerates case, accents, punctuation, '#'/'N°'/'no', underscores, camelCase, plurals, units in the title,
English / French / Italian (some German / Spanish) wording and small typos. Examples that all mean the
purchase-order number: "PO", "Purchase order", "purchase_order_#", "N° commande", "Bon de commande",
"Ordine fornitore". A bare "Order" / "Commande" is decided by the other columns of the table
(customer, ship date, truck -> customer order; otherwise purchase order).

Units in the title are converted: "Length (cm)" -> length_mm x 10, "Weight (g)" -> weight_kg / 1000.
"""
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

# field -> synonyms (any language; normalised like the headers)
SYNONYMS: dict[str, list[str]] = {
    "po_number": ["po", "purchase order", "purchase order number", "order po", "bon de commande", "bc",
                  "commande fournisseur", "commande achat", "ordine fornitore", "ordine di acquisto", "ordine acquisto",
                  "oda", "bestellung lieferant", "orden de compra", "supplier order", "vendor order"],
    "line_ref": ["line", "line ref", "line item", "ligne", "riga", "pos", "position", "posizione", "posicion",
                 "zeile"],
    "supplier": ["supplier", "vendor", "fournisseur", "fornitore", "lieferant", "proveedor"],
    "sku": ["sku", "article", "article code", "item code", "part number", "part", "reference", "ref",
            "reference article", "code article", "codice", "codice articolo", "articolo", "artikel", "material",
            "product code", "product", "produit", "prodotto", "item"],
    "description": ["description", "desc", "designation", "libelle", "descrizione", "denominazione",
                    "bezeichnung", "product name", "article name", "nom article"],
    "qty_units": ["qty", "quantity", "quantite", "qte", "quantita", "qta", "unit", "delivered qty", "qty delivered",
                  "menge", "cantidad", "piece", "pcs", "colli", "handling unit", "nb unit"],
    "packaging": ["packaging", "pack", "package", "unit type", "conditionnement", "emballage", "imballo",
                  "confezione", "verpackung", "packing"],
    "length_mm": ["length", "longueur", "lunghezza", "lange", "laenge", "largo", "l", "len"],
    "width_mm": ["width", "largeur", "larghezza", "breite", "ancho", "w"],
    "height_mm": ["height", "hauteur", "altezza", "hoehe", "hohe", "alto", "h"],
    "weight_kg": ["weight", "poids", "peso", "gewicht", "gross weight", "mass", "massa", "unit weight"],
    "fragility": ["fragile", "fragility", "fragilite", "fragilita", "zerbrechlich"],
    "expected_date": ["expected date", "expected delivery", "eta date", "due date", "date livraison prevue",
                      "livraison prevue", "data consegna prevista", "consegna prevista", "expected"],
    "dn_number": ["delivery note", "dn", "ddt", "bl", "bon de livraison", "bolla", "packing slip",
                  "documento di trasporto", "lieferschein", "albaran"],
    "delivery_date": ["delivery date", "delivered on", "date livraison", "data consegna", "received date",
                      "receipt date", "date reception"],
    "order_number": ["customer order", "sales order", "so", "cpo", "commande client", "ordine cliente",
                     "client order", "customer po", "kundenauftrag", "pedido cliente"],
    "customer": ["customer", "client", "cliente", "kunde", "destinataire", "destinatario", "consignee"],
    "ship_at": ["ship date", "ship time", "ship at", "shipping date", "ship datetime", "ship", "date expedition",
                "expedition", "data spedizione", "spedizione", "dispatch date", "dispatch", "versanddatum"],
    "truck_ref": ["truck", "truck ref", "trip", "camion", "lorry", "vehicle", "viaggio", "tour", "trailer"],
    "carrier": ["carrier", "haulier", "transporteur", "vettore", "spediteur", "forwarder", "transporter",
                "trasportatore"],
    "direction": ["direction", "in out", "sens", "flow", "type"],
    "dock": ["dock", "door", "quai", "baia", "porta", "gate", "rampe"],
    "arrival_at": ["arrival", "eta", "arrivee", "arrivo", "ankunft", "arrival time"],
    "departure_at": ["departure", "etd", "depart", "partenza", "abfahrt", "departure time"],
    "case_id": ["case", "case id", "location", "bin location", "emplacement", "ubicazione", "slot",
                "storage location", "bin", "lagerplatz"],
    # handling unit incl. packaging (stock lists)
    "length_m": ["unit length incl pack", "l incl pack", "handling unit length"],
    "width_m": ["unit width incl pack", "w incl pack", "handling unit width"],
    "height_m": ["unit height incl pack", "h incl pack", "handling unit height"],
    "unit_kg": ["unit incl pack", "unit weight incl pack", "weight incl pack", "handling unit weight"],
}
GENERIC_ORDER = {"order", "commande", "ordine", "orden", "bestellung", "ordre", "auftrag", "pedido"}
CUSTOMER_CONTEXT = {"customer", "ship_at", "truck_ref"}

STOP = {"no", "nr", "n", "num", "number", "numero", "nbr", "nb", "id", "the", "of", "de", "du", "des", "la", "le",
        "les", "di", "del", "della", "d", "per", "par", "en", "a", "da", "von", "der", "die", "incl_"}
LENGTH_UNITS = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "mt": 1000.0, "in": 25.4, "inch": 25.4}
WEIGHT_UNITS = {"kg": 1.0, "g": 0.001, "gr": 0.001, "t": 1000.0, "lb": 0.4536, "lbs": 0.4536}
DIM_FIELDS = {"length_mm", "width_mm", "height_mm"}
DIM_M_FIELDS = {"length_m", "width_m", "height_m"}
WEIGHT_FIELDS = {"weight_kg", "unit_kg"}
DATE_FIELDS = {"expected_date", "delivery_date", "ship_at", "arrival_at", "departure_at"}
DATE_TOKENS = {"date", "data", "datum", "fecha", "jour", "giorno", "day", "time", "heure", "ora"}
MIN_SCORE = 0.75


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _singular(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t


@lru_cache(maxsize=4096)
def tokenize(text: str) -> tuple[tuple[str, ...], str | None]:
    """-> (meaningful tokens, unit found in the title or None)."""
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text))          # camelCase
    s = _strip_accents(s).lower().replace("°", " no ").replace("º", " no ").replace("#", " no ")
    s = s.replace("'", " ").replace("/", " ")
    raw = [t for t in re.split(r"[^a-z0-9]+", s) if t]
    unit = None
    tokens = []
    for t in raw:
        if t in LENGTH_UNITS or t in WEIGHT_UNITS:
            if unit is None:
                unit = t
            if t not in ("in", "t", "g", "m") or len(raw) > 1:     # a lone 'm'/'t'/'g' may still be a word
                continue
        if t == "incl" or t == "including" or t == "inclus":
            tokens.append("incl")
            continue
        if t in STOP:
            continue
        tokens.append(_singular(t))
    return tuple(tokens), unit


@lru_cache(maxsize=1)
def _synonym_index() -> list[tuple[str, tuple[str, ...]]]:
    out = []
    for field, syns in SYNONYMS.items():
        for syn in syns + [field.replace("_", " ")]:
            toks, _ = tokenize(syn)
            if toks:
                out.append((field, toks))
    return out


def _unit_ok(field: str, unit: str | None) -> bool:
    if unit is None:
        return True
    if unit in WEIGHT_UNITS and unit not in LENGTH_UNITS:
        return field in WEIGHT_FIELDS
    if unit in LENGTH_UNITS and unit not in WEIGHT_UNITS:
        return field in DIM_FIELDS | DIM_M_FIELDS
    return True


def scores(header: str) -> dict[str, float]:
    """field -> confidence 0..1 for one column title."""
    toks, unit = tokenize(header)
    out: dict[str, float] = {}
    if not toks:                                    # title is only a unit: "kg", "mm"
        if unit in WEIGHT_UNITS:
            out["weight_kg"] = 0.9
        return out
    tset, joined = set(toks), " ".join(toks)
    has_date = bool(tset & DATE_TOKENS)
    for field, syn in _synonym_index():
        sset = set(syn)
        if tset == sset:
            s = 1.0
        elif sset < tset:
            s = 0.92 - 0.04 * (len(tset) - len(sset))
        elif len(joined) >= 4 and len(" ".join(syn)) >= 4:
            r = SequenceMatcher(None, joined, " ".join(syn)).ratio()
            s = r * 0.9 if r >= 0.84 else 0.0                 # typos: "quanity", "suplier"
        else:
            s = 0.0
        if s and has_date and field not in DATE_FIELDS and not sset & DATE_TOKENS:
            s -= 0.3                                         # "PO date" is not the PO number
        if s and not _unit_ok(field, unit):
            s = 0.0
        if s > out.get(field, 0.0):
            out[field] = s
    return out


def _is_generic_order(header: str) -> bool:
    toks, _ = tokenize(header)
    return bool(toks) and set(toks) <= GENERIC_ORDER


def scale_for(field: str, header: str) -> float:
    """Multiply values by this to get the field's unit (mm, kg, m)."""
    _, unit = tokenize(header)
    if unit is None:
        return 1.0
    if field in DIM_FIELDS and unit in LENGTH_UNITS:
        return LENGTH_UNITS[unit]
    if field in DIM_M_FIELDS and unit in LENGTH_UNITS:
        return LENGTH_UNITS[unit] / 1000.0
    if field in WEIGHT_FIELDS and unit in WEIGHT_UNITS:
        return WEIGHT_UNITS[unit]
    return 1.0


def slug(header: str) -> str:
    toks, _ = tokenize(header)
    return "_".join(toks) or re.sub(r"\W+", "_", str(header).lower()).strip("_") or "col"


def resolve(headers: list[str]) -> list[tuple[str, float]]:
    """Whole table at once -> [(field or slug, scale)] per column. Each field is used once (best match
    wins); a bare 'Order' column is a customer order if the table has customer / ship date / truck columns."""
    cand = [(s, f, i) for i, h in enumerate(headers) for f, s in scores(h).items() if s >= MIN_SCORE]
    cand.sort(key=lambda t: (-t[0], t[2]))
    field_of: dict[int, str] = {}
    used: set[str] = set()
    generic = {i for i, h in enumerate(headers) if _is_generic_order(h)}
    for s, f, i in cand:
        if i in field_of or f in used or i in generic:
            continue
        field_of[i] = f
        used.add(f)
    for i in sorted(generic):
        f = "order_number" if (used & CUSTOMER_CONTEXT) else "po_number"
        if f not in used:
            field_of[i] = f
            used.add(f)
    out = []
    taken: dict[str, int] = {}
    for i, h in enumerate(headers):
        f = field_of.get(i)
        if f is None:
            name = slug(h)
            taken[name] = taken.get(name, 0) + 1
            out.append((name if taken[name] == 1 else f"{name}_{taken[name]}", 1.0))
        else:
            out.append((f, scale_for(f, h)))
    return out


def best_field(header: str) -> str:
    """Single title without table context (best guess)."""
    return resolve([header])[0][0]
