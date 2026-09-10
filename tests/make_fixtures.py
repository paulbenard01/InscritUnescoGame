"""Generate recorded-shaped SPARQL/Commons responses for offline pipeline tests.

Each case exercises something that broke, or would break, against live data:
cross-product rows, missing coordinates, thin fr/es coverage, non-ASCII names,
id collisions, and non-commercial licences.
"""
import json, os

OUT = os.path.join(os.path.dirname(__file__), "fixtures")
E = "http://www.wikidata.org/entity/"

def uri(q): return {"type": "uri", "value": E + q}
def lit(v, lang=None):
    c = {"type": "literal", "value": str(v)}
    if lang: c["xml:lang"] = lang
    return c

def row(**kw):
    r = {}
    for k, v in kw.items():
        if v is None: continue
        r[k] = v
    return r

# --- material sites -------------------------------------------------------
material = [
    # Cross product: one item, two images x two criteria = 4 rows to be merged.
    row(item=uri("Q170471"), labelEn=lit("Göreme National Park", "en"),
        labelFr=lit("Parc national de Göreme", "fr"), labelEs=lit("Parque Nacional de Göreme", "es"),
        descEn=lit("Volcanic rock landscape in Türkiye", "en"),
        descFr=lit("Paysage de roches volcaniques", "fr"),
        altEn=lit("Cappadocia", "en"),
        lat=lit(38.65), lon=lit(34.83), sitelinks=lit(52), inscribed=lit("1985-01-01T00:00:00Z"),
        image=uri("").copy() if False else {"type":"uri","value":"http://commons.wikimedia.org/wiki/Special:FilePath/Goreme.jpg"},
        country=uri("Q43"), countryEn=lit("Türkiye","en"), countryFr=lit("Turquie","fr"), countryEs=lit("Turquía","es"),
        continent=uri("Q48"), continentEn=lit("Asia","en"), criterionEn=lit("World Heritage selection criterion (i)","en")),
    row(item=uri("Q170471"), labelEn=lit("Göreme National Park", "en"),
        lat=lit(38.65), lon=lit(34.83), sitelinks=lit(52),
        image={"type":"uri","value":"http://commons.wikimedia.org/wiki/Special:FilePath/Goreme_2.jpg"},
        country=uri("Q43"), countryEn=lit("Türkiye","en"),
        criterionEn=lit("World Heritage selection criterion (vii)","en")),

    # Natural-only criteria, no fr/es label at all -> English fallback counted.
    row(item=uri("Q41225"), labelEn=lit("Sagarmatha National Park","en"),
        descEn=lit("National park in Nepal","en"),
        lat=lit(27.98), lon=lit(86.92), sitelinks=lit(31), inscribed=lit("1979-01-01T00:00:00Z"),
        country=uri("Q837"), countryEn=lit("Nepal","en"), countryFr=lit("Népal","fr"), countryEs=lit("Nepal","es"),
        continentEn=lit("Asia","en"), criterionEn=lit("World Heritage selection criterion (vii)","en")),

    # Non-commercial image; also no continent (P30 absent -> country fallback).
    row(item=uri("Q131013"), labelEn=lit("Historic Centre of Sighisoara","en"),
        labelFr=lit("Centre historique de Sighisoara","fr"),
        lat=lit(46.22), lon=lit(24.79), sitelinks=lit(9), inscribed=lit("1999-01-01T00:00:00Z"),
        image={"type":"uri","value":"http://commons.wikimedia.org/wiki/Special:FilePath/Sighisoara.jpg"},
        country=uri("Q218"), countryEn=lit("Romania","en"), countryFr=lit("Roumanie","fr"), countryEs=lit("Rumania","es"),
        criterionEn=lit("World Heritage selection criterion (iii)","en")),

    # Same English name as another entry -> id collision must be disambiguated.
    row(item=uri("Q999001"), labelEn=lit("Historic Centre of Sighisoara","en"),
        lat=lit(45.0), lon=lit(25.0), sitelinks=lit(2),
        country=uri("Q218"), countryEn=lit("Romania","en")),

    # No name at all -> must be dropped.
    row(item=uri("Q999002"), lat=lit(1.0), lon=lit(1.0), sitelinks=lit(0)),
]

# --- intangible elements --------------------------------------------------
immaterial = [
    # No coordinate anywhere -> country centroid fallback (task 2).
    row(item=uri("Q193036"), labelEn=lit("Flamenco","en"), labelFr=lit("Flamenco","fr"),
        labelEs=lit("Flamenco","es"), descEn=lit("Spanish art form","en"),
        descEs=lit("Arte español","es"), sitelinks=lit(78), inscribed=lit("2010-11-16T00:00:00Z"),
        image={"type":"uri","value":"http://commons.wikimedia.org/wiki/Special:FilePath/Flamenco.jpg"},
        country=uri("Q29"), countryEn=lit("Spain","en"), countryFr=lit("Espagne","fr"), countryEs=lit("España","es")),

    # No coordinate, no fr/es labels, low sitelinks -> tier 3 + English fallback.
    row(item=uri("Q999003"), labelEn=lit("Al-Ayyala, a traditional performing art","en"),
        sitelinks=lit(3), inscribed=lit("2014-11-27T00:00:00Z"),
        country=uri("Q878"), countryEn=lit("United Arab Emirates","en"),
        countryFr=lit("Émirats arabes unis","fr"), countryEs=lit("Emiratos Árabes Unidos","es")),

    row(item=uri("Q1042"), labelEn=lit("Yoga","en"), labelFr=lit("Le yoga","fr"), labelEs=lit("El yoga","es"),
        descEn=lit("Indian physical and mental practice","en"), sitelinks=lit(64),
        country=uri("Q668"), countryEn=lit("India","en"), countryFr=lit("Inde","fr"), countryEs=lit("India","es"),
        continentEn=lit("Asia","en")),
]

country_coords = [
    row(country=uri("Q29"), lat=lit(40.0), lon=lit(-4.0)),
    row(country=uri("Q878"), lat=lit(24.0), lon=lit(54.0)),
    row(country=uri("Q668"), lat=lit(21.0), lon=lit(78.0)),
]
country_continents = [
    row(country=uri("Q218"), continentEn=lit("Europe","en")),
    row(country=uri("Q29"), continentEn=lit("Europe","en")),
    row(country=uri("Q878"), continentEn=lit("Asia","en")),
]

commons = {
    "Goreme.jpg": {"thumb_url": "https://upload.wikimedia.org/x/Goreme.jpg", "license": "CC BY-SA 4.0",
                   "artist": "A. Photographer", "credit": "Own work",
                   "descriptionurl": "https://commons.wikimedia.org/wiki/File:Goreme.jpg"},
    "Flamenco.jpg": {"thumb_url": "https://upload.wikimedia.org/x/Flamenco.jpg", "license": "CC BY 3.0",
                     "artist": "B. Shooter", "credit": "", "descriptionurl": ""},
    "Sighisoara.jpg": {"thumb_url": "https://upload.wikimedia.org/x/Sighisoara.jpg",
                       "license": "CC BY-NC-SA 2.0", "artist": "C. Snapper", "credit": "",
                       "descriptionurl": ""},
}

def wrap(rows): return {"results": {"bindings": rows}}

os.makedirs(OUT, exist_ok=True)
for name, payload in [("Q9259", wrap(material)), ("Q1459900", wrap(immaterial)),
                      ("country_coords", wrap(country_coords)),
                      ("country_continents", wrap(country_continents)),
                      ("commons", commons)]:
    with open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
print("wrote fixtures to", OUT)
