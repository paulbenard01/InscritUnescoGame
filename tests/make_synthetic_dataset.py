"""Generate a full-size synthetic dataset.json for front-end testing.

Stands in for the real pipeline output while Wikimedia egress is blocked: same
shape, same field optionality (missing years, missing descriptions, missing
photos, country-centroid coordinates), at realistic scale.
"""
import json, os, random, sys

random.seed(7)
N_MAT, N_IMM = 1273, 849
CONTINENTS = ["EU", "AS", "AF", "NA", "SA", "OC"]
COUNTRIES = {f"Q{100+i}": {"en": f"Country {i}", "fr": f"Pays {i}", "es": f"País {i}"} for i in range(60)}
CATS_M = ["cultural", "natural", "mixed", "site"]

def make(i, kind):
    q = f"Q{900000+i}"
    name = f"{'Site' if kind=='material' else 'Tradition'} {i}"
    sit = int(random.paretovariate(1.2))
    e = {"id": f"{kind[:3]}-{i}", "qid": q, "type": kind,
         "names": {"en": name, "fr": f"{name} (fr)" if i % 3 else name,
                   "es": f"{name} (es)" if i % 4 else name},
         "lat": round(random.uniform(-55, 70), 4), "lng": round(random.uniform(-179, 179), 4),
         "sitelinks": sit,
         "continent": random.choice(CONTINENTS),
         "category": random.choice(CATS_M) if kind == "material" else "intangible",
         "country": random.choice(list(COUNTRIES)),
         "aliases": [name, f"alias{i}"]}
    if i % 3:
        e["desc"] = {"en": f"A description of {name}.", "fr": f"Une description de {name}.",
                     "es": f"Una descripción de {name}."}
    if i % 5:
        e["year"] = random.randint(1978, 2024)
    if kind == "immaterial" and i % 2:
        e["approx"] = True
    if i % 4:
        e["image"] = {"path": f"images/{kind[:3]}-{i}.jpg", "file": f"F{i}.jpg",
                      "license": "CC BY-SA 4.0", "credit": f"Photographer {i}"}
    return e

entries = [make(i, "material") for i in range(N_MAT)] + [make(i, "immaterial") for i in range(N_IMM)]

# Tier with the pipeline's own function, so the fixture reflects the real split
# (and this doubles as a scale test of assign_tiers on a long-tailed distribution).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from build_dataset import assign_tiers
assign_tiers(entries)

entries.sort(key=lambda e: -e["sitelinks"])
out = sys.argv[1] if len(sys.argv) > 1 else "data/dataset.json"
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    json.dump({"meta": {"count": len(entries), "synthetic": True},
               "countries": COUNTRIES, "entries": entries}, f, ensure_ascii=False, separators=(",", ":"))
print(f"wrote {len(entries)} synthetic entries to {out} "
      f"({os.path.getsize(out)/1024:.0f} KB)")
