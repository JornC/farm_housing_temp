# Emissiefactoren stalsystemen

**Let op: voorlopig en niet gevalideerd.** Deze omzetting is gemaakt zonder
inhoudelijke domeinkennis, puur op basis van de gepubliceerde wettekst, en is
bedoeld als concept in afwachting van de fatsoenlijke, formele datalevering.
Gebruik de uitvoer niet zonder inhoudelijke validatie.

Zet de tekst-export van Bijlage V en VI van de Omgevingsregeling (BWBR0045528,
wetten.overheid.nl) om naar machineleesbare databestanden (dbdata-formaat).
Deterministisch: zelfde invoer geeft byte-identieke uitvoer en ambiguiteiten
in de bron worden gerapporteerd in plaats van stilzwijgend opgelost.

Het script is geschreven met een LLM (Claude Code); de omzetting zelf is
volledig mechanisch, zonder taalmodel- of handwerkstap. Dat is na te gaan
door het script opnieuw te draaien: de uitvoer is reproduceerbaar uit de
bronbestanden.

## Gebruik

```
python3 extract_stalsystemen.py
```

Alleen Python-standaardbibliotheek, geen netwerk.

## Indeling

| pad | inhoud |
|---|---|
| `extract_stalsystemen.py` | het extractiescript |
| `bron-wettekst/` | tekst-exports van Bijlage V en VI ("Opslaan als tekst") |
| `referentie-dbdata/` | de 6 dbdata-bestanden van de vorige levering |
| `output/` | gegenereerde uitvoer, reproduceerbaar met het script |

## Uitvoer

Zes dbdata-bestanden (tab-gescheiden, CRLF, ook als `.gz`), getagd
`_PRELIMINARY_UNVALIDATED` - voorlopig, niet gevalideerd, in afwachting van
de formele levering:

| bestand | bron | inhoud |
|---|---|---|
| `farm_animal_categories` | Bijlage V | 36 diercategorieen |
| `farm_animal_housing_categories` | Bijlage V | 233 huisvestingssystemen |
| `farm_housing_emission_factors` | Bijlage V | NH3-factor per systeem |
| `farm_additional_housing_systems` | Bijlage VI | 47 aanvullende technieken |
| `farm_additional_housing_factors` | Bijlage VI | NH3-reductie per techniek x systeem |
| `farm_housing_categories_additional_systems` | Bijlage VI | koppeltabel toepasbaarheid |

Ter review: `bijlage_v_*.tsv` en `bijlage_vi_*.tsv` (incl. geur- en
fijnstofwaarden die niet in de dbdata-set zitten), `rapport.txt`
(verschillen en ambiguiteiten) en `diffs/farm_*.diff` (unified diff per
bestand t.o.v. de vorige levering).

## Werking

- Fixed-width parsing; kolombreedte afgeleid uit de kopregel. Onverwachte
  invoer is een harde fout, er wordt niet gegokt.
- Render-artefacten van de export worden genormaliseerd (non-breaking
  spaces, sub-/superscript als los token: `m 2`, `PM 10`).
- De export breekt lange woorden af zonder koppelteken; ambigue gevallen
  komen in het rapport en worden waar mogelijk beslecht via de vorige
  levering.
- Bijlage VI-variantrijen (afwijkende percentages voor bv. HC/HK) worden op
  celinhoud toegewezen; "toepasbaar bij" wordt geexpandeerd naar individuele
  systemen (`HD` = alles onder HD1..HD5, `HE1` = `HE1.*`, `HD1.100` = exact).

## Herkomst

Alle inhoud (codes, teksten, factoren, toepasbaarheid, reducties) komt uit de
bijlagen. Uit de vorige levering komt alleen wat de wettekst niet bevat:
numerieke id's, `farm_animal_type`, `farm_emission_factor_type`,
`air_scrubber` en `substance_id` (17 = NH3). Factorwaarden worden nooit
overgenomen; de oude bestanden dienen alleen voor het verschillenrapport.

## Validatie (bijlagen 29-05-2026 vs levering 20260310)

- Bijlage VI-expansie reproduceert de vorige levering exact: 3160 paren en
  reductiefactoren, 0 verschil.
- 233 = 233 systemen, 36 = 36 diercategorieen, 47 = 47 technieken; niets
  bijgekomen of vervallen.
- Enige factorwijziging: HA1.34 van 8,3 naar 8 kg NH3/jaar (consistent met
  V1 -> V2 en de ingekorte omschrijving).
- Verder: twee datumcorrecties (HD3.10, HD5.14) en drie redactionele
  verschillen in diercategorie-omschrijvingen.

Deze controles zijn technisch van aard; inhoudelijke validatie door de
betrokken partijen blijft nodig voordat de uitvoer ergens voor wordt gebruikt.
