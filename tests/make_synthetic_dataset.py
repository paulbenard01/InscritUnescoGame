"""Generate a full-size synthetic dataset.json for front-end testing.

Stands in for the real pipeline output while Wikimedia egress is blocked: same
shape, same field optionality (missing years, missing descriptions, missing
photos, country-centroid coordinates), at realistic scale.
"""
import base64, json, os, random, sys

random.seed(7)
N_MAT, N_IMM = 1273, 849
CONTINENTS = ["EU", "AS", "AF", "NA", "SA", "OC"]
# Countries are the guessing vocabulary and every guess is measured from the
# country's centroid, so they need coordinates.
COUNTRIES = {
    f"Q{100+i}": {"en": f"Country {i}", "fr": f"Pays {i}", "es": f"País {i}",
                  "cont": CONTINENTS[i % len(CONTINENTS)],
                  "lat": round(random.uniform(-50, 65), 4),
                  "lng": round(random.uniform(-170, 170), 4)}
    for i in range(60)
}
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

# A real, decodable 96x60 JPEG. It has to actually decode: the game only marks
# the photo zoomable on a successful load, so a malformed blob silently
# disables the viewer and the test that covers it.
SAMPLE_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIf"
    "IiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7"
    "Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAARCAA8AGADASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDgViqR"
    "YqsLFT1irtczmhUIFiqRYqnWKpFirNzO2FQgWKpFiqdYqkWKs3M7YVCusVSLFVhYqesVZOZ2wqEC"
    "xVIsVTrFUixVm5nZCoQLFT1iqwsVSLFWbmdsKhgLFUixVOsVSLFXS5n5fCoQLFT1iqwsVSLFWTmd"
    "sKhXWKpFiqdYqkWKs3M7YVCBYqkWKp1iqRYqzczthUIFip6xVYWKpFirJzOyFQrrFUixVOsVSLFW"
    "bmdsKhgLFUixVYWKpFirpcz8vhUK6xVIsVTrFUixVm5nbCoQLFUixVOsVSLFWTmdsKhXWKpFiqws"
    "VSLFWbmdkKhXWKpFiqdYqkWKs3M7YVCBYqkWKp1iqRYqyczthUMBYqkWKplUU9VFdTmfmEJsiWKp"
    "FiqVVFSKorNzO2E2QrFUixVMqipFUVk5nZCbIViqRYqlVRUiqKzczthNkSxU9YqmVRUiqKyczthN"
    "kKxVIsVTKoqRVFZuZ2wmz//Z")
# A real (tiny but decodable) JPEG per entry that claims a photo, so the image
# path, the onerror placeholder and the zoom viewer are all genuinely exercised.
img_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(out))), "images")
os.makedirs(img_dir, exist_ok=True)
written = 0
for e in entries:
    if "image" in e:
        with open(os.path.join(img_dir, f"{e['id']}.jpg"), "wb") as f:
            f.write(SAMPLE_JPEG)
        written += 1
print(f"wrote {written} placeholder JPEGs to {img_dir}")
