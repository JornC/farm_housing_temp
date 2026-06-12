#!/usr/bin/env python3
"""Extractie van huisvestingssystemen, emissiefactoren en aanvullende
technieken uit de tekst-export van Bijlage V en VI van de Omgevingsregeling
(BWBR0045528), zoals gedownload van wetten.overheid.nl, naar de
dbdata-bestanden.

Deterministisch en herhaalbaar: zelfde invoer -> byte-identieke uitvoer.
Geen netwerk, geen taalmodelstap, alleen de lokale bronbestanden.

VOORLOPIG EN NIET GEVALIDEERD: gemaakt zonder inhoudelijke domeinkennis,
puur op basis van de gepubliceerde wettekst, in afwachting van de formele
datalevering. Uitvoer niet gebruiken zonder inhoudelijke validatie.

Invoer (automatisch gevonden naast het script):
  - bron-wettekst/*BijlageV-*.txt   : tekst-export Bijlage V  (huisvestingssystemen + emissiefactoren)
  - bron-wettekst/*BijlageVI-*.txt  : tekst-export Bijlage VI (aanvullende technieken + reductiepercentages)
  - referentie-dbdata/farm_*_<datum>.txt.gz : de dbdata-bestanden van de vorige
    levering, gebruikt voor id-toekenning, opmaakconventies en het verschillenrapport:
      farm_animal_categories, farm_animal_housing_categories,
      farm_housing_emission_factors, farm_additional_housing_systems,
      farm_additional_housing_factors, farm_housing_categories_additional_systems

Uitvoer (./output/), elk ook als .gz in het leveringsformaat:
  - farm_animal_categories_<datum>_PRELIMINARY_UNVALIDATED.txt
  - farm_animal_housing_categories_<datum>_PRELIMINARY_UNVALIDATED.txt
  - farm_housing_emission_factors_<datum>_PRELIMINARY_UNVALIDATED.txt
  - farm_additional_housing_systems_<datum>_PRELIMINARY_UNVALIDATED.txt
  - farm_additional_housing_factors_<datum>_PRELIMINARY_UNVALIDATED.txt
  - farm_housing_categories_additional_systems_<datum>_PRELIMINARY_UNVALIDATED.txt
  plus ter review:
  - bijlage_v_huisvestingssystemen.tsv (incl. geur- en fijnstoffactoren)
  - bijlage_vi_aanvullende_technieken.tsv (incl. geur- en fijnstofreducties)
  - rapport.txt (verschillen t.o.v. de vorige levering + alle ambiguiteiten)

De tekst-export van wetten.overheid.nl rendert de tabel in vaste kolommen en
breekt lange woorden af ZONDER koppelteken. Dit script reconstrueert cellen
volgens de greedy-wrap-semantiek van die renderer; elk geval waarin twee
lezingen mogelijk zijn wordt gerapporteerd en waar mogelijk beslecht door
vergelijking met de vorige levering.
"""

from datetime import date
import difflib
import glob
import gzip
import os
import re

# ---------------------------------------------------------------------------
# Generieke parser voor de fixed-width tabelexport
# ---------------------------------------------------------------------------

# Een logische rij begint op een regel waarvan de eerste cel volledig matcht
# met een code-achtige waarde. Alles daarvoor (titel, kolomkoppen, eenheden)
# wordt overgeslagen; daarna is een niet-matchende eerste cel een fout.
ROW_START = re.compile(
    r"^(?:"
    r"HOOFDCATEGORIE .+"          # categorie-kop in Bijlage V
    r"|[A-Z]{1,4}\d[\d.]*"        # code: HA1, HD1.3.1, LW1.1, AP100.3, ...
    r"|[A-Z]{2,4}"                # letter-groep: LW, AR, AV, AP, en variantrijen HC, HD, HK
    r"|[A-Z]{1,4}\d[\d.]*(?:, ?[A-Z]{1,4}[\d.]*)+,?"  # codelijst in variantrijen
    r"|OW \d{4}\.\d+(?:\.V\d+)?"  # systeembeschrijvingsnummer in variantrijen
    r")$"
)


class Ambiguity:
    """Een celregel die de kolom exact vult, gevolgd door een vervolgwoord
    waarmee het samengevoegde token langer dan de kolom zou zijn: zowel
    'twee woorden' als 'afgebroken woord' reproduceren de bron exact."""

    def __init__(self, source, row_code, with_space, without_space):
        self.source = source
        self.row_code = row_code
        self.with_space = with_space
        self.without_space = without_space
        self.resolution = None  # ingevuld als vergelijking met vorige levering beslist


def join_fragments(fragments, content_width, source, row_code, ambiguities):
    """Voeg de visuele regels van een cel samen tot de oorspronkelijke tekst."""
    text = fragments[0].strip()
    pending = []  # indices in `text` waar een ambigue spatie is ingevoegd
    for i, frag in enumerate(fragments[1:], start=1):
        nxt = frag.strip()
        prev_full = len(fragments[i - 1].rstrip()) >= content_width
        last_tok = text.rsplit(" ", 1)[-1]
        first_tok = nxt.split(" ", 1)[0]
        if prev_full and len(last_tok + first_tok) > content_width:
            # Greedy wrap breekt alleen woorden af die langer zijn dan de
            # kolom; beide lezingen zijn mogelijk -> registreren.
            pending.append(len(text))
        text = text + " " + nxt
    result_ambiguities = []
    for pos in pending:
        with_space = text
        without_space = text[:pos] + text[pos + 1:]
        amb = Ambiguity(source, row_code, with_space, without_space)
        ambiguities.append(amb)
        result_ambiguities.append(amb)
    return text, result_ambiguities


def parse_fixed_width_table(path, ambiguities):
    """Parse de tekst-export naar logische rijen van samengevoegde cellen.

    Retourneert een lijst van (cells, cell_ambiguities) met cells een lijst
    strings (lege cellen = ''). Kolombreedte en -aantal worden afgeleid uit
    de kopregel die met 'Code' begint.
    """
    with open(path, encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f]

    header_idx = next(i for i, ln in enumerate(lines) if ln.startswith("Code "))
    header = lines[header_idx]
    # kolombreedte = positie van het tweede kopwoord (Beschrijving/Omschrijving)
    width = re.search(r"^Code\s+", header).end()
    n_cols = max((len(ln) + width - 1) // width for ln in lines[header_idx:])
    content_width = width - 2  # twee spaties scheiding tussen kolommen

    rows = []           # elk: lijst van fragmentlijsten per kolom
    current = None
    for ln in lines[header_idx + 1:]:
        if not ln.strip():
            continue
        cells = [ln[i * width:(i + 1) * width].strip() for i in range(n_cols)]
        raw_cells = [ln[i * width:(i + 1) * width] for i in range(n_cols)]
        # detecteer content die over een kolomgrens heen loopt (mag niet voorkomen,
        # behalve bij regels die de hele tabelbreedte beslaan zoals HOOFDCATEGORIE)
        for i in range(1, n_cols):
            if len(ln) > i * width and ln[i * width - 1] != " " and ln[i * width] != " ":
                if not ln.startswith("HOOFDCATEGORIE"):
                    raise SystemExit(
                        f"FOUT: celinhoud loopt over kolomgrens in {path}: {ln!r}")
        if cells[0] and ROW_START.fullmatch(cells[0]):
            current = [[c] if c else [] for c in raw_cells]
            rows.append(current)
        elif current is not None and any(cells):
            if cells[0]:
                raise SystemExit(
                    f"FOUT: onverwachte inhoud in codekolom in {path}: {cells[0]!r}")
            for i, c in enumerate(raw_cells):
                if c.strip():
                    current[i].append(c)
        # regels voor de eerste logische rij (kolomkoppen/eenheden) worden overgeslagen

    source = os.path.basename(path)
    result = []
    for fragments in rows:
        row_code = (fragments[0][0].strip() if fragments[0] else "")
        cells = []
        cell_ambs = []
        for frags in fragments:
            if not frags:
                cells.append("")
                continue
            text, ambs = join_fragments(frags, content_width, source, row_code,
                                        ambiguities)
            cells.append(text)
            cell_ambs.extend(ambs)
        result.append((cells, cell_ambs))
    return result


# ---------------------------------------------------------------------------
# Normalisatie van render-artefacten van de tekst-export
# ---------------------------------------------------------------------------

def normalize_rendering(text):
    """Sub-/superscript wordt door de export als los token gerenderd
    ('m 2', 'PM 10', 'NH 3') en getal-eenheid wordt met een non-breaking
    space gescheiden; plak die weer vast en herstel de spatiering."""
    text = text.replace(" ", " ")
    text = re.sub(r"\b(m|PM|NH) (\d+) ?(?=/)", r"\1\2", text)
    text = re.sub(r"\b(m|PM|NH) (\d+)\b", r"\1\2", text)
    text = re.sub(r"  +", " ", text)
    text = re.sub(r" ([,.;:])", r"\1", text)
    return text.strip()


def comparison_key(text):
    """Normalisatie uitsluitend voor het vergelijken met de vorige levering:
    en-dash vs koppelteken en aanhalingstekens zijn cosmetisch."""
    text = normalize_rendering(text)
    text = text.replace("–", "-").replace("‘", "'").replace("’", "'")
    return text


def parse_number(value):
    """Nederlandse notatie uit de bijlage ('5,7', '11,0', '0,210') naar de
    notatie van de dbdata-bestanden ('5.7', '11', '0.21')."""
    s = value.replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", s):
        raise SystemExit(f"FOUT: onverwacht getalsformaat: {value!r}")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def pct_to_fraction(value):
    """Reductiepercentage ('70%', '0%') naar fractie ('0.7', '0')."""
    m = re.fullmatch(r"(\d+)%", value)
    if not m:
        raise SystemExit(f"FOUT: onverwacht percentage: {value!r}")
    s = f"{int(m.group(1)) / 100:.2f}".rstrip("0").rstrip(".")
    return s


# ---------------------------------------------------------------------------
# Bijlage V: huisvestingssystemen met emissiefactoren
# ---------------------------------------------------------------------------

def parse_bijlage_v(path, ambiguities):
    rows = parse_fixed_width_table(path, ambiguities)
    systems = []
    animal_categories = []
    hoofdcategorie = None
    diercategorie = None
    tussenkoppen = {}  # code -> omschrijving, voor context van geneste codes
    for cells, cell_ambs in rows:
        code = cells[0]
        if code.startswith("HOOFDCATEGORIE"):
            hoofdcategorie = normalize_rendering(code)
            diercategorie = None
            continue
        has_factors = any(c for c in cells[3:6])
        if not has_factors:
            if "." not in code:
                diercategorie = (code, normalize_rendering(cells[1]))
                animal_categories.append({
                    "code": code,
                    "omschrijving": diercategorie[1],
                    "hoofdcategorie": hoofdcategorie,
                    "_ambiguities": cell_ambs,
                })
            else:
                tussenkoppen[code] = normalize_rendering(cells[1])
            continue
        if diercategorie is None or not code.startswith(diercategorie[0]):
            raise SystemExit(f"FOUT: systeem {code} valt buiten diercategorie "
                             f"{diercategorie}")
        parent = code.rsplit(".", 1)[0]
        systems.append({
            "hoofdcategorie": hoofdcategorie,
            "diercategorie_code": diercategorie[0],
            "diercategorie_omschrijving": diercategorie[1],
            "tussenkop": tussenkoppen.get(parent, ""),
            "code": code,
            "beschrijving": normalize_rendering(cells[1]),
            "nummer_systeembeschrijving": normalize_rendering(cells[2]),
            "ef_ammoniak_kg_nh3_per_jaar": cells[3],
            "ef_geur_oue_per_sec": cells[4],
            "ef_fijnstof_g_pm10_per_jaar": cells[5],
            "_ambiguities": cell_ambs,
        })
    return systems, animal_categories


# ---------------------------------------------------------------------------
# Bijlage VI: aanvullende technieken met reductiepercentages
# ---------------------------------------------------------------------------

PCT = re.compile(r"^(\d+(?:–\d+)?%|[-–])$")
OWNUM = re.compile(r"^OW \d{4}\.\d+(?:\.V\d+)?(, OW \d{4}\.\d+(?:\.V\d+)?)*$")


def parse_bijlage_vi(path, ambiguities):
    rows = parse_fixed_width_table(path, ambiguities)
    techniques = []
    groep = None        # bv. LW: Luchtwassystemen
    subgroep = None     # bv. LW1: Enkelvoudige biologische luchtwassystemen
    last_leaf = None
    for cells, cell_ambs in rows:
        code = cells[0]
        is_code = re.fullmatch(r"[A-Z]{1,4}\d*[\d.]*", code) and not OWNUM.match(code)
        has_pct = any(PCT.match(c) for c in cells if c)
        if is_code and not has_pct:
            if "." not in code:
                if re.fullmatch(r"[A-Z]{2,4}", code):
                    groep = (code, normalize_rendering(cells[1]))
                    subgroep = None
                else:
                    subgroep = (code, normalize_rendering(cells[1]))
            continue
        if is_code and "." in code:
            # hoofdrij van een techniek: kolommen staan op hun vaste plaats
            last_leaf = {
                "code": code,
                "omschrijving": normalize_rendering(cells[1]),
                "nummer_systeembeschrijving": normalize_rendering(cells[2]),
            }
            techniques.append(dict(
                last_leaf,
                groep_code=groep[0], groep_omschrijving=groep[1],
                subgroep_code=subgroep[0] if subgroep else "",
                subgroep_omschrijving=subgroep[1] if subgroep else "",
                toepasbaar_bij=normalize_rendering(cells[3]),
                reductie_ammoniak=cells[4], reductie_geur=cells[5],
                reductie_fijnstof=cells[6],
                voldoen_ook_aan_nummer=normalize_rendering(cells[7]) if len(cells) > 7 else "",
                _ambiguities=cell_ambs,
            ))
            continue
        # variantrij: de export schuift gevulde cellen naar links; ken cellen
        # toe op inhoud (OW-nummer / codelijst / percentages / OW-nummer)
        if last_leaf is None:
            raise SystemExit(f"FOUT: variantrij zonder voorafgaande techniek: {cells}")
        filled = [c for c in cells if c]
        pct_idx = [i for i, c in enumerate(filled) if PCT.match(c)]
        if len(pct_idx) != 3:
            raise SystemExit(f"FOUT: variantrij zonder 3 percentages: {cells}")
        before = filled[:pct_idx[0]]
        after = filled[pct_idx[2] + 1:]
        nummer = last_leaf["nummer_systeembeschrijving"]
        toepasbaar = ""
        for c in before:
            if OWNUM.match(c):
                nummer = normalize_rendering(c)
            else:
                toepasbaar = normalize_rendering(c)
        voldoen = ""
        for c in after:
            voldoen = normalize_rendering(c)
        techniques.append(dict(
            code=last_leaf["code"],
            omschrijving=last_leaf["omschrijving"],
            nummer_systeembeschrijving=nummer,
            groep_code=groep[0], groep_omschrijving=groep[1],
            subgroep_code=subgroep[0] if subgroep else "",
            subgroep_omschrijving=subgroep[1] if subgroep else "",
            toepasbaar_bij=toepasbaar,
            reductie_ammoniak=filled[pct_idx[0]], reductie_geur=filled[pct_idx[1]],
            reductie_fijnstof=filled[pct_idx[2]],
            voldoen_ook_aan_nummer=voldoen,
            _ambiguities=cell_ambs,
        ))
    return techniques


# ---------------------------------------------------------------------------
# dbdata-bestanden van de vorige levering
# ---------------------------------------------------------------------------

class Reference:
    def __init__(self, path):
        self.path = path
        with gzip.open(path, "rb") as f:
            raw = f.read()
        self.line_ending = "\r\n" if b"\r\n" in raw else "\n"
        lines = [ln for ln in raw.decode("utf-8").split(self.line_ending) if ln]
        self.header = lines[0].split("\t")
        self.rows = [dict(zip(self.header, ln.split("\t"))) for ln in lines[1:]]


def find_one(pattern):
    matches = sorted(glob.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(f"FOUT: verwacht precies 1 bestand voor {pattern!r}, "
                         f"gevonden: {matches}")
    return matches[0]


def single_value(rows, column):
    values = {r[column] for r in rows}
    if len(values) != 1:
        raise SystemExit(f"FOUT: verwacht 1 unieke waarde voor {column}, "
                         f"gevonden: {sorted(values)}")
    return values.pop()


# ---------------------------------------------------------------------------
# Toepasbaar-bij-expansie (Bijlage VI -> stalsysteemcodes)
# ---------------------------------------------------------------------------

def expand_toepasbaar(spec, housing_codes, context):
    """'HA3, HD, HE1.1.2.1' -> set van stalsysteemcodes. Een token van alleen
    letters is een hoofdcategorie-prefix (HD -> HD1..HD5); met cijfers is het
    een exacte code of een prefix op codegrens (HE1 -> HE1.*)."""
    matched = set()
    for token in [t.strip() for t in spec.split(",") if t.strip()]:
        if re.fullmatch(r"[A-Z]+", token):
            hits = {c for c in housing_codes if re.match(rf"{token}\d", c)}
        else:
            hits = {c for c in housing_codes
                    if c == token or c.startswith(token + ".")}
        if not hits:
            raise SystemExit(f"FOUT: 'toepasbaar bij'-code {token!r} ({context}) "
                             f"matcht geen enkel stalsysteem")
        matched |= hits
    return matched


# ---------------------------------------------------------------------------
# Uitvoer
# ---------------------------------------------------------------------------

def write_tsv(path, header, rows, line_ending="\n"):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\t".join(header) + line_ending)
        for row in rows:
            f.write("\t".join(str(row.get(col, "")) for col in header) + line_ending)


def write_dbdata(path, reference, rows):
    """Schrijf in exact het formaat van het referentiebestand (kolommen,
    regeleindes) en maak de .gz-variant; mtime=0 houdt herhaalde runs
    byte-identiek. Schrijft daarnaast een unified diff t.o.v. de vorige
    levering in output/diffs/."""
    write_tsv(path, reference.header, rows, line_ending=reference.line_ending)
    with open(path, "rb") as fin, \
            gzip.GzipFile(path + ".gz", "wb", mtime=0) as fout:
        fout.write(fin.read())

    old_lines = ["\t".join(reference.header)] + [
        "\t".join(r[c] for c in reference.header) for r in reference.rows]
    new_lines = ["\t".join(str(r.get(c, "")) for c in reference.header)
                 for r in rows]
    new_lines = ["\t".join(reference.header)] + new_lines
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=os.path.basename(reference.path).removesuffix(".gz"),
        tofile=os.path.basename(path), lineterm="")
    os.makedirs("output/diffs", exist_ok=True)
    table = re.sub(r"_\d{8}.*$", "", os.path.basename(path))
    with open(f"output/diffs/{table}.diff", "w", encoding="utf-8") as f:
        f.write("\n".join(diff) + "\n")


def diff_rows(old_rows, new_rows, key_cols, compare_cols):
    """Vergelijk rijensets op sleutel; retourneert (nieuw, vervallen, gewijzigd)
    met gewijzigd = (sleutel, kolom, oud, nieuw)."""
    def key(row):
        return tuple(row[c] for c in key_cols)
    old_by_key = {key(r): r for r in old_rows}
    new_by_key = {key(r): r for r in new_rows}
    added = [k for k in new_by_key if k not in old_by_key]
    removed = [k for k in old_by_key if k not in new_by_key]
    changed = []
    for k, new in new_by_key.items():
        old = old_by_key.get(k)
        if old is None:
            continue
        for col in compare_cols:
            if old[col] != str(new[col]):
                changed.append((k, col, old[col], new[col]))
    return added, removed, changed


def main():
    workdir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(workdir)
    path_v = find_one("bron-wettekst/*BijlageV-*.txt")
    path_vi = find_one("bron-wettekst/*BijlageVI-*.txt")
    refs = {name: Reference(find_one(f"referentie-dbdata/farm_{name}_*.txt.gz"))
            for name in [
        "animal_categories", "animal_housing_categories",
        "housing_emission_factors", "additional_housing_systems",
        "additional_housing_factors", "housing_categories_additional_systems"]}
    os.makedirs("output", exist_ok=True)

    # datumstempel van vandaag in de bestandsnamen, met expliciete markering
    # dat dit een ongevalideerde conceptlevering is
    stamp = date.today().strftime("%Y%m%d") + "_PRELIMINARY_UNVALIDATED"

    ambiguities = []
    systems, animal_cats = parse_bijlage_v(path_v, ambiguities)
    techniques = parse_bijlage_vi(path_vi, ambiguities)

    report = []

    def section(title):
        report.append("")
        report.append("=" * 78)
        report.append(title)
        report.append("=" * 78)

    # --- ambiguiteiten beslechten via de vorige levering --------------------
    prev_housing = {r["code"]: r for r in refs["animal_housing_categories"].rows}
    prev_additional = {r["code"]: r for r in refs["additional_housing_systems"].rows}
    prev_animal = {r["code"]: r for r in refs["animal_categories"].rows}

    def resolve(row, text_field, prev_description):
        for amb in row["_ambiguities"]:
            for candidate, label in ((amb.with_space, "met spatie"),
                                     (amb.without_space, "zonder spatie")):
                if comparison_key(candidate) == comparison_key(prev_description):
                    amb.resolution = f"{label} (gelijk aan vorige levering)"
                    row[text_field] = normalize_rendering(candidate)
                    return

    for s in systems:
        if s["code"] in prev_housing:
            resolve(s, "beschrijving", prev_housing[s["code"]]["description"])
    for t in techniques:
        if t["code"] in prev_additional:
            resolve(t, "omschrijving", prev_additional[t["code"]]["description"])
    for a in animal_cats:
        if a["code"] in prev_animal:
            # vorige levering heeft de omschrijving zonder het woord 'Diercategorie'
            resolve(a, "omschrijving",
                    "Diercategorie " + prev_animal[a["code"]]["description"])

    # --- review-TSV's van beide bijlagen ------------------------------------
    write_tsv("output/bijlage_v_huisvestingssystemen.tsv",
              ["code", "beschrijving", "nummer_systeembeschrijving",
               "ef_ammoniak_kg_nh3_per_jaar", "ef_geur_oue_per_sec",
               "ef_fijnstof_g_pm10_per_jaar", "diercategorie_code",
               "diercategorie_omschrijving", "tussenkop", "hoofdcategorie"],
              systems)
    write_tsv("output/bijlage_vi_aanvullende_technieken.tsv",
              ["code", "omschrijving", "nummer_systeembeschrijving",
               "toepasbaar_bij", "reductie_ammoniak", "reductie_geur",
               "reductie_fijnstof", "voldoen_ook_aan_nummer",
               "subgroep_code", "subgroep_omschrijving",
               "groep_code", "groep_omschrijving"],
              techniques)

    # --- farm_animal_categories ----------------------------------------------
    # description = omschrijving zonder het voorvoegsel 'Diercategorie';
    # farm_animal_type komt uit de vorige levering, voor nieuwe categorieen
    # uit een bestaande categorie met dezelfde hoofdcategorie-letter.
    type_by_letter = {}
    for r in refs["animal_categories"].rows:
        type_by_letter.setdefault(re.match(r"H([A-Z])", r["code"]).group(1),
                                  r["farm_animal_type"])
    next_animal_id = max(int(r["farm_animal_category_id"])
                         for r in refs["animal_categories"].rows) + 1
    out_animal = []
    for a in animal_cats:
        desc = re.sub(r"^Diercategorie\s+", "", a["omschrijving"])
        desc = desc[:1].upper() + desc[1:]
        prev = prev_animal.get(a["code"])
        if prev:
            row_id, animal_type = prev["farm_animal_category_id"], prev["farm_animal_type"]
        else:
            row_id = str(next_animal_id)
            next_animal_id += 1
            letter = re.match(r"H([A-Z])", a["code"]).group(1)
            if letter not in type_by_letter:
                raise SystemExit(f"FOUT: geen farm_animal_type bekend voor "
                                 f"nieuwe hoofdcategorie {a['code']}")
            animal_type = type_by_letter[letter]
        out_animal.append({
            "farm_animal_category_id": row_id, "code": a["code"],
            "farm_animal_type": animal_type, "name": a["code"],
            "description": desc,
        })
    out_animal.sort(key=lambda r: int(r["farm_animal_category_id"]))
    write_dbdata(f"output/farm_animal_categories_{stamp}.txt",
                 refs["animal_categories"], out_animal)
    added, removed, changed = diff_rows(
        refs["animal_categories"].rows, out_animal,
        ["code"], ["farm_animal_category_id", "farm_animal_type", "description"])
    section(f"farm_animal_categories: {len(out_animal)} rijen "
            f"(vorige levering {len(refs['animal_categories'].rows)})")
    report.append(f"nieuw: {added or 'geen'}  vervallen: {removed or 'geen'}")
    for k, col, old, new in changed:
        report.append(f"  ~ {k[0]} {col}:")
        report.append(f"      oud : {old}")
        report.append(f"      nieuw: {new}")
    if not changed:
        report.append("gewijzigd: geen")

    # --- farm_animal_housing_categories --------------------------------------
    animal_id_by_code = {r["code"]: r["farm_animal_category_id"] for r in out_animal}
    eftype_by_animal = {}
    for r in refs["animal_housing_categories"].rows:
        eftype_by_animal.setdefault(r["farm_animal_category_id"],
                                    r["farm_emission_factor_type"])
    next_housing_id = max(int(r["farm_animal_housing_category_id"])
                          for r in refs["animal_housing_categories"].rows) + 1
    out_housing = []
    for s in systems:
        prev = prev_housing.get(s["code"])
        animal_id = animal_id_by_code[s["diercategorie_code"]]
        if prev:
            row_id = prev["farm_animal_housing_category_id"]
            ef_type = prev["farm_emission_factor_type"]
            if prev["farm_animal_category_id"] != animal_id:
                raise SystemExit(f"FOUT: {s['code']} wisselt van diercategorie")
        else:
            row_id = str(next_housing_id)
            next_housing_id += 1
            ef_type = eftype_by_animal.get(animal_id, "per_animal_per_year")
        out_housing.append({
            "farm_animal_housing_category_id": row_id,
            "farm_animal_category_id": animal_id,
            "code": s["code"], "name": s["code"],
            "description": s["beschrijving"],
            "farm_emission_factor_type": ef_type,
        })
        s["_id"] = row_id
    out_housing.sort(key=lambda r: int(r["farm_animal_housing_category_id"]))
    write_dbdata(f"output/farm_animal_housing_categories_{stamp}.txt",
                 refs["animal_housing_categories"], out_housing)
    added, removed, changed = diff_rows(
        refs["animal_housing_categories"].rows, out_housing,
        ["code"], ["farm_animal_housing_category_id", "farm_animal_category_id",
                   "description", "farm_emission_factor_type"])
    substantive = [c for c in changed
                   if comparison_key(c[2]) != comparison_key(str(c[3]))]
    cosmetic = [c for c in changed if c not in substantive]
    section(f"farm_animal_housing_categories: {len(out_housing)} rijen "
            f"(vorige levering {len(refs['animal_housing_categories'].rows)})")
    report.append(f"nieuw: {[k[0] for k in added] or 'geen'}  "
                  f"vervallen: {[k[0] for k in removed] or 'geen'}")
    report.append(f"inhoudelijk gewijzigd ({len(substantive)}):")
    for k, col, old, new in substantive:
        report.append(f"  ~ {k[0]} {col}:")
        report.append(f"      oud : {old}")
        report.append(f"      nieuw: {new}")
    report.append(f"alleen cosmetisch (interpunctie/leestekens, {len(cosmetic)}):")
    for k, col, old, new in cosmetic:
        report.append(f"  ~ {k[0]}: {old!r} -> {new!r}")

    # --- farm_housing_emission_factors ---------------------------------------
    substance_nh3 = single_value(refs["housing_emission_factors"].rows, "substance_id")
    out_factors = []
    for s in systems:
        out_factors.append({
            "farm_animal_housing_category_id": s["_id"],
            "substance_id": substance_nh3,
            "emission_factor": parse_number(s["ef_ammoniak_kg_nh3_per_jaar"]),
        })
    out_factors.sort(key=lambda r: int(r["farm_animal_housing_category_id"]))
    write_dbdata(f"output/farm_housing_emission_factors_{stamp}.txt",
                 refs["housing_emission_factors"], out_factors)
    added, removed, changed = diff_rows(
        refs["housing_emission_factors"].rows, out_factors,
        ["farm_animal_housing_category_id"], ["substance_id", "emission_factor"])
    code_by_id = {r["farm_animal_housing_category_id"]: r["code"] for r in out_housing}
    section(f"farm_housing_emission_factors: {len(out_factors)} rijen "
            f"(vorige levering {len(refs['housing_emission_factors'].rows)})")
    report.append(f"nieuw: {[code_by_id.get(k[0], k[0]) for k in added] or 'geen'}  "
                  f"vervallen: {removed or 'geen'}")
    report.append(f"GEWIJZIGDE EMISSIEFACTOREN ({len(changed)}):")
    for k, col, old, new in changed:
        report.append(f"  ~ {code_by_id.get(k[0], k[0])} (id {k[0]}): {old} -> {new}")
    if not changed:
        report.append("  geen")

    # --- farm_additional_housing_systems --------------------------------------
    # air_scrubber: uit de vorige levering; voor nieuwe technieken volgt het
    # uit de groep in Bijlage VI (LW = luchtwassystemen).
    unique_techniques = {}
    for t in techniques:
        unique_techniques.setdefault(t["code"], t)
    next_add_id = max(int(r["farm_additional_housing_system_id"])
                      for r in refs["additional_housing_systems"].rows) + 1
    out_additional = []
    for code, t in unique_techniques.items():
        prev = prev_additional.get(code)
        if prev:
            row_id, scrubber = prev["farm_additional_housing_system_id"], prev["air_scrubber"]
        else:
            row_id = str(next_add_id)
            next_add_id += 1
            scrubber = "t" if t["groep_code"] == "LW" else "f"
        out_additional.append({
            "farm_additional_housing_system_id": row_id, "code": code,
            "name": code, "description": t["omschrijving"],
            "air_scrubber": scrubber,
        })
    out_additional.sort(key=lambda r: int(r["farm_additional_housing_system_id"]))
    write_dbdata(f"output/farm_additional_housing_systems_{stamp}.txt",
                 refs["additional_housing_systems"], out_additional)
    added, removed, changed = diff_rows(
        refs["additional_housing_systems"].rows, out_additional,
        ["code"], ["farm_additional_housing_system_id", "description", "air_scrubber"])
    substantive = [c for c in changed
                   if comparison_key(c[2]) != comparison_key(str(c[3]))]
    cosmetic = [c for c in changed if c not in substantive]
    section(f"farm_additional_housing_systems: {len(out_additional)} rijen "
            f"(vorige levering {len(refs['additional_housing_systems'].rows)})")
    report.append(f"nieuw: {[k[0] for k in added] or 'geen'}  "
                  f"vervallen: {[k[0] for k in removed] or 'geen'}")
    report.append(f"inhoudelijk gewijzigd ({len(substantive)}):")
    for k, col, old, new in substantive:
        report.append(f"  ~ {k[0]} {col}:")
        report.append(f"      oud : {old}")
        report.append(f"      nieuw: {new}")
    report.append(f"alleen cosmetisch (interpunctie/leestekens, {len(cosmetic)}):")
    for k, col, old, new in cosmetic:
        report.append(f"  ~ {k[0]}: {old!r} -> {new!r}")

    # --- farm_additional_housing_factors + koppeltabel ------------------------
    # 'toepasbaar bij' expanderen naar individuele stalsystemen; het
    # NH3-reductiepercentage van de betreffende (variant)rij geldt voor alle
    # stalsystemen die de rij aanwijst.
    add_id_by_code = {r["code"]: r["farm_additional_housing_system_id"]
                      for r in out_additional}
    housing_codes = [s["code"] for s in systems]
    housing_id_by_code = {s["code"]: s["_id"] for s in systems}
    substance_add = single_value(refs["additional_housing_factors"].rows, "substance_id")
    pair_factor = {}
    for t in techniques:
        if not t["toepasbaar_bij"]:
            raise SystemExit(f"FOUT: techniek {t['code']} zonder 'toepasbaar bij'")
        fraction = pct_to_fraction(t["reductie_ammoniak"])
        for code in expand_toepasbaar(t["toepasbaar_bij"], housing_codes, t["code"]):
            pair = (t["code"], code)
            if pair in pair_factor and pair_factor[pair] != fraction:
                raise SystemExit(f"FOUT: conflicterende reductie voor {pair}: "
                                 f"{pair_factor[pair]} vs {fraction}")
            pair_factor[pair] = fraction

    out_add_factors = [{
        "farm_additional_housing_system_id": add_id_by_code[tcode],
        "farm_animal_housing_category_id": housing_id_by_code[hcode],
        "substance_id": substance_add,
        "reduction_factor": fraction,
    } for (tcode, hcode), fraction in pair_factor.items()]
    out_add_factors.sort(key=lambda r: (int(r["farm_additional_housing_system_id"]),
                                        int(r["farm_animal_housing_category_id"])))
    write_dbdata(f"output/farm_additional_housing_factors_{stamp}.txt",
                 refs["additional_housing_factors"], out_add_factors)

    out_links = [{
        "farm_animal_housing_category_id": r["farm_animal_housing_category_id"],
        "farm_additional_housing_system_id": r["farm_additional_housing_system_id"],
    } for r in out_add_factors]
    out_links.sort(key=lambda r: (int(r["farm_animal_housing_category_id"]),
                                  int(r["farm_additional_housing_system_id"])))
    write_dbdata(f"output/farm_housing_categories_additional_systems_{stamp}.txt",
                 refs["housing_categories_additional_systems"], out_links)

    add_code_by_id = {v: k for k, v in add_id_by_code.items()}

    def pair_name(system_id, housing_id):
        return f"{add_code_by_id.get(system_id, system_id)}+{code_by_id.get(housing_id, housing_id)}"

    added, removed, changed = diff_rows(
        refs["additional_housing_factors"].rows, out_add_factors,
        ["farm_additional_housing_system_id", "farm_animal_housing_category_id",
         "substance_id"], ["reduction_factor"])
    section(f"farm_additional_housing_factors: {len(out_add_factors)} rijen "
            f"(vorige levering {len(refs['additional_housing_factors'].rows)})")
    report.append(f"nieuwe paren ({len(added)}): "
                  f"{[pair_name(k[0], k[1]) for k in added] or 'geen'}")
    report.append(f"vervallen paren ({len(removed)}): "
                  f"{[pair_name(k[0], k[1]) for k in removed] or 'geen'}")
    report.append(f"GEWIJZIGDE REDUCTIEFACTOREN ({len(changed)}):")
    for k, col, old, new in changed:
        report.append(f"  ~ {pair_name(k[0], k[1])}: {old} -> {new}")
    if not changed:
        report.append("  geen")

    added, removed, changed = diff_rows(
        refs["housing_categories_additional_systems"].rows, out_links,
        ["farm_animal_housing_category_id", "farm_additional_housing_system_id"], [])
    section(f"farm_housing_categories_additional_systems: {len(out_links)} rijen "
            f"(vorige levering {len(refs['housing_categories_additional_systems'].rows)})")
    report.append(f"nieuwe paren ({len(added)}): "
                  f"{[pair_name(k[1], k[0]) for k in added] or 'geen'}")
    report.append(f"vervallen paren ({len(removed)}): "
                  f"{[pair_name(k[1], k[0]) for k in removed] or 'geen'}")

    # --- ambiguiteiten ---------------------------------------------------------
    section(f"AMBIGUE WOORDAFBREKINGEN in de tekst-export ({len(ambiguities)})")
    report.append("(regel vulde de kolom exact; 'woord afgebroken' en 'twee")
    report.append(" woorden' zijn beide mogelijk; controleer waar niet beslecht)")
    for amb in ambiguities:
        report.append(f"  ? {amb.source} rij {amb.row_code}:")
        report.append(f"      met spatie   : ...{amb.with_space[-60:]}")
        report.append(f"      zonder spatie: ...{amb.without_space[-60:]}")
        report.append(f"      beslecht     : {amb.resolution or 'NEE - handmatig controleren'}")

    header = [
        "RAPPORT extractie Bijlage V en VI Omgevingsregeling (BWBR0045528)",
        "",
        "VOORLOPIG EN NIET GEVALIDEERD: gemaakt zonder inhoudelijke",
        "domeinkennis, puur op basis van de gepubliceerde wettekst, in",
        "afwachting van de formele datalevering.",
        "",
        f"Bron Bijlage V : {path_v}",
        f"Bron Bijlage VI: {path_vi}",
        "Referentie     : dbdata-levering "
        + refs["animal_housing_categories"].path.split("_")[-1].split(".")[0],
        "",
        f"Bijlage V : {len(systems)} huisvestingssystemen, "
        f"{len(animal_cats)} diercategorieen",
        f"Bijlage VI: {len(unique_techniques)} aanvullende technieken "
        f"({len(techniques)} tabelrijen, {len(pair_factor)} "
        f"techniek-stalsysteem-paren)",
    ]
    text = "\n".join(header + report) + "\n"
    with open("output/rapport.txt", "w", encoding="utf-8") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()
