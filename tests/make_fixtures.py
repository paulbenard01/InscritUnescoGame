"""Generate recorded-shaped SPARQL/Commons responses for offline pipeline tests.

Mirrors the aggregated query shape: one row per item, with multi-valued fields
GROUP_CONCAT'd on SEP and coordinates paired into a single "lat,lon" literal.

Each case exercises something that broke, or would break, against live data:
missing coordinates, thin fr/es coverage, non-ASCII names, id collisions,
dual-designated items, malformed coordinates, and non-commercial licences.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from build_dataset import SEP

OUT = os.path.join(os.path.dirname(__file__), "fixtures")
E = "http://www.wikidata.org/entity/"
FP = "http://commons.wikimedia.org/wiki/Special:FilePath/"


def uri(q):
    return {"type": "uri", "value": E + q}


def lit(v, lang=None):
    c = {"type": "literal", "value": str(v)}
    if lang:
        c["xml:lang"] = lang
    return c


def cat(vals):
    """A GROUP_CONCAT binding."""
    return {"type": "literal", "value": SEP.join(str(v) for v in vals)}


def row(**kw):
    return {k: v for k, v in kw.items() if v is not None}


# --- material sites -------------------------------------------------------
material = [
    # Non-ASCII name; two images and two criteria (cultural + natural -> mixed).
    row(item=uri("Q170471"),
        labelEn=lit("Göreme National Park", "en"),
        labelFr=lit("Parc national de Göreme", "fr"),
        labelEs=lit("Parque Nacional de Göreme", "es"),
        descEn=lit("Volcanic rock landscape in Türkiye", "en"),
        descFr=lit("Paysage de roches volcaniques", "fr"),
        coord=lit("38.65,34.83"), sitelinks=lit(52),
        inscribed=lit("1985-01-01T00:00:00Z"),
        country=cat([E + "Q43"]),
        continentEn=cat(["Asia"]),
        image=cat([FP + "Goreme.jpg", FP + "Goreme_2.jpg"]),
        criterionEn=cat(["World Heritage selection criterion (i)",
                         "World Heritage selection criterion (vii)"])),

    # Natural-only criteria; no fr/es label at all -> English fallback counted.
    row(item=uri("Q41225"),
        labelEn=lit("Sagarmatha National Park", "en"),
        descEn=lit("National park in Nepal", "en"),
        coord=lit("27.98,86.92"), sitelinks=lit(31),
        inscribed=lit("1979-01-01T00:00:00Z"),
        country=cat([E + "Q837"]), continentEn=cat(["Asia"]),
        criterionEn=cat(["World Heritage selection criterion (vii)"])),

    # Non-commercial image; no P30 -> continent must come from the country.
    row(item=uri("Q131013"),
        labelEn=lit("Historic Centre of Sighisoara", "en"),
        labelFr=lit("Centre historique de Sighisoara", "fr"),
        coord=lit("46.22,24.79"), sitelinks=lit(9),
        inscribed=lit("1999-01-01T00:00:00Z"),
        country=cat([E + "Q218"]),
        image=cat([FP + "Sighisoara.jpg"]),
        criterionEn=cat(["World Heritage selection criterion (iii)"])),

    # Same English name as the entry above -> id collision must disambiguate.
    row(item=uri("Q999001"),
        labelEn=lit("Historic Centre of Sighisoara", "en"),
        coord=lit("45.0,25.0"), sitelinks=lit(2),
        country=cat([E + "Q218"])),

    # No name at all -> dropped, counted as no_name.
    row(item=uri("Q999002"), coord=lit("1.0,1.0"), sitelinks=lit(0)),

    # Malformed coordinate and no country -> dropped, counted as no_coord.
    row(item=uri("Q999004"), labelEn=lit("Unplaceable Site", "en"),
        coord=lit("not-a-coord"), sitelinks=lit(1)),

    # Also appears under the intangible designation below (dual designation).
    row(item=uri("Q999005"), labelEn=lit("Dual Designated Thing", "en"),
        coord=lit("10.0,10.0"), sitelinks=lit(7), country=cat([E + "Q29"])),
]

# --- intangible elements --------------------------------------------------
immaterial = [
    # No coordinate -> country centroid fallback, flagged approx.
    row(item=uri("Q193036"),
        labelEn=lit("Flamenco", "en"), labelFr=lit("Flamenco", "fr"),
        labelEs=lit("Flamenco", "es"),
        descEn=lit("Spanish art form", "en"), descEs=lit("Arte español", "es"),
        sitelinks=lit(78), inscribed=lit("2010-11-16T00:00:00Z"),
        country=cat([E + "Q29"]), image=cat([FP + "Flamenco.jpg"])),

    # No coordinate, no fr/es labels, low sitelinks -> tier 3 + English fallback.
    row(item=uri("Q999003"),
        labelEn=lit("Al-Ayyala, a traditional performing art", "en"),
        sitelinks=lit(3), inscribed=lit("2014-11-27T00:00:00Z"),
        country=cat([E + "Q878"])),

    row(item=uri("Q1042"),
        labelEn=lit("Yoga", "en"), labelFr=lit("Le yoga", "fr"), labelEs=lit("El yoga", "es"),
        descEn=lit("Indian physical and mental practice", "en"),
        sitelinks=lit(64), country=cat([E + "Q668"]), continentEn=cat(["Asia"])),

    # Duplicate of the material entry above -- must keep the material one.
    row(item=uri("Q999005"), labelEn=lit("Dual Designated Thing", "en"),
        coord=lit("10.0,10.0"), sitelinks=lit(7), country=cat([E + "Q29"])),
]

aliases = [
    row(item=uri("Q170471"), alt=lit("Cappadocia", "en")),
    row(item=uri("Q170471"), alt=lit("Cappadoce", "fr")),
    row(item=uri("Q193036"), alt=lit("Flamenco dance", "en")),
]

country_info = [
    row(country=uri("Q43"), cEn=lit("Türkiye", "en"), cFr=lit("Turquie", "fr"),
        cEs=lit("Turquía", "es"), coord=lit("39.0,35.0"), contEn=lit("Asia", "en")),
    row(country=uri("Q837"), cEn=lit("Nepal", "en"), cFr=lit("Népal", "fr"),
        cEs=lit("Nepal", "es"), coord=lit("28.0,84.0"), contEn=lit("Asia", "en")),
    row(country=uri("Q218"), cEn=lit("Romania", "en"), cFr=lit("Roumanie", "fr"),
        cEs=lit("Rumania", "es"), coord=lit("46.0,25.0"), contEn=lit("Europe", "en")),
    row(country=uri("Q29"), cEn=lit("Spain", "en"), cFr=lit("Espagne", "fr"),
        cEs=lit("España", "es"), coord=lit("40.0,-4.0"), contEn=lit("Europe", "en")),
    row(country=uri("Q878"), cEn=lit("United Arab Emirates", "en"),
        cFr=lit("Émirats arabes unis", "fr"), cEs=lit("Emiratos Árabes Unidos", "es"),
        coord=lit("24.0,54.0"), contEn=lit("Asia", "en")),
    row(country=uri("Q668"), cEn=lit("India", "en"), cFr=lit("Inde", "fr"),
        cEs=lit("India", "es"), coord=lit("21.0,78.0"), contEn=lit("Asia", "en")),
]

commons = {
    "Goreme.jpg": {"thumb_url": "https://upload.wikimedia.org/x/Goreme.jpg",
                   "license": "CC BY-SA 4.0", "artist": "A. Photographer",
                   "credit": "Own work",
                   "descriptionurl": "https://commons.wikimedia.org/wiki/File:Goreme.jpg"},
    "Flamenco.jpg": {"thumb_url": "https://upload.wikimedia.org/x/Flamenco.jpg",
                     "license": "CC BY 3.0", "artist": "B. Shooter", "credit": "",
                     "descriptionurl": ""},
    "Sighisoara.jpg": {"thumb_url": "https://upload.wikimedia.org/x/Sighisoara.jpg",
                       "license": "CC BY-NC-SA 2.0", "artist": "C. Snapper",
                       "credit": "", "descriptionurl": ""},
}


def wrap(rows):
    return {"results": {"bindings": rows}}


os.makedirs(OUT, exist_ok=True)
for name, payload in [("Q9259", wrap(material)), ("Q1459900", wrap(immaterial)),
                      ("aliases", wrap(aliases)), ("country_info", wrap(country_info)),
                      ("commons", commons)]:
    with open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
print("wrote fixtures to", OUT)
