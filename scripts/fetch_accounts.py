#!/usr/bin/env python3
"""Pull US building-materials suppliers from Overture Maps Places and group branches into companies.

Runs weekly in GitHub Actions and writes data/accounts.json.
Needs: pip install pyarrow
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.fs as pafs

BUCKET = "overturemaps-us-west-2"
LIST_URL = f"https://{BUCKET}.s3.us-west-2.amazonaws.com/?list-type=2&prefix=release/&delimiter=/"

STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM",
    "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
}

# Overture categories that can hold a supplier. Name rules below do the real filtering.
CANDIDATE_TAX = {"building_supply_store", "lumber_store", "wholesaler", "hardware_store",
                 "home_improvement_store", "roofing"}

# A supplier name says what it sells and that it sells it.
SUPPLIER = re.compile(
    r"\b(suppl(y|ies)|materials?|lumber|building products|millwork|distribut\w*|wholesale|"
    r"forest products|builders? supply|wallboard|gypsum|lumberyard|lbm)\b", re.I)
# Words that mark a contractor, service business or retailer, not a distributor.
NOT_SUPPLIER = re.compile(
    r"\b(contract(ing|ors?)|restoration|remodel\w*|repair|install\w*|landscap\w*|lawn|tree|"
    r"handyman|heating|cooling|hvac|plumb\w*|electric\w*|excavat\w*|rental|rentals|homes|"
    r"realty|church|school|salon|lashes|cabinet|countertops?|granite|flooring|carpet|tile|"
    r"pool|spa|fence|fencing|irrigation|mulch|nursery|garden|auto|truck|tire|beauty|"
    r"office supply|medical|dental|janitorial|restaurant|food|beverage|pet|plumbing|"
    r"electrical|welding|safety|industrial supply|paint)\b", re.I)

SEGMENT_NAME = [
    ("roofing", re.compile(r"\broof\w*|\bsiding\b|exterior|shingle|gutter", re.I)),
    ("gypsum", re.compile(r"gypsum|drywall|wallboard|interior supply|wall systems|insulation|acoustic|ceiling|\bl ?& ?w\b|\bgms\b|foundation building", re.I)),
    ("lumber", re.compile(r"lumber|timber|millwork|forest products|truss|\bwood\b|hardwood|plywood", re.I)),
]

NATIONAL_CHAINS = [
    ("ABC Supply", r"\babc supply|\bbradco\b"), ("Beacon / QXO", r"\bbeacon\b|\bqxo\b|allied building products"),
    ("SRS Distribution", r"\bsrs\b|suncoast roofers|heritage roofing"), ("The Home Depot", r"home depot"),
    ("Lowe's", r"\blowe'?s\b"), ("Menards", r"menards"), ("84 Lumber", r"\b84 lumber"),
    ("Builders FirstSource", r"builders firstsource|\bbmc\b|probuild"), ("L&W Supply", r"\bl ?& ?w supply"),
    ("GMS", r"\bgms\b|gypsum management"), ("Foundation Building Materials", r"foundation building materials|\bfbm\b"),
    ("Ferguson", r"ferguson"), ("US LBM", r"\bus lbm\b"), ("Ace Hardware", r"\bace hardware"),
    ("True Value", r"true value"), ("Do it Best", r"do it best"), ("Tractor Supply", r"tractor supply"),
    ("Fastenal", r"fastenal"), ("Grainger", r"grainger"), ("Sherwin-Williams", r"sherwin"),
    ("White Cap", r"white cap"), ("Lansing Building Products", r"lansing building products"),
]
NATIONAL_RE = [(n, re.compile(p, re.I)) for n, p in NATIONAL_CHAINS]

# From the customer logo wall on ventura.ai (checked 2026-11-09).
VENTURA_CUSTOMERS = [
    "Action Gypsum", "Bone Roofing Supply", "Dairyman's Supply", "Gemco Roofing & Building Supply",
    "Heely Brown", "Industrial Metal Supply", "Lakeside Roofing & Siding Materials",
    "New Castle Building Products", "Richards Building Supply", "Roofers Mart", "SPEC Building Materials",
]

SUFFIX = re.compile(r"\b(inc|incorporated|llc|l l c|co|corp|corporation|company|ltd|lp|the|of [a-z]+)\b", re.I)
GENERIC = {"supply", "building supply", "lumber", "building materials", "materials", "construction materials",
           "roofing supply", "lumber company", "supply company"}


def norm(name):
    s = name.lower().replace("’", "'").replace("&", " and ")
    s = re.sub(r"\s+[-–|@,]\s*.*$", "", s)          # "Acme Lumber - Columbus", "Acme Lumber, Dayton"
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


CUSTOMER_KEYS = {norm(c): c for c in VENTURA_CUSTOMERS}


def domain(url):
    if not url:
        return None
    m = re.match(r"^(?:[a-z]+://)?([^/:?#]+)", url.strip().lower())
    if not m:
        return None
    host = re.sub(r"^www\d?\.", "", m.group(1))
    if host in {"facebook.com", "google.com", "yelp.com", "business.site", "linkedin.com", "instagram.com",
                "sites.google.com", "wixsite.com", "godaddysites.com", "square.site", "yellowpages.com"}:
        return None
    return host


def segment(name):
    for seg, rx in SEGMENT_NAME:
        if rx.search(name):
            return seg
    return "building"


def latest_release():
    with urllib.request.urlopen(LIST_URL, timeout=60) as r:
        xml = r.read().decode()
    rels = sorted(re.findall(r"<Prefix>release/(\d{4}-\d{2}-\d{2}\.\d+)/</Prefix>", xml))
    if not rels:
        raise SystemExit("no Overture releases found")
    return rels[-1]


def main():
    release = sys.argv[1] if len(sys.argv) > 1 else latest_release()
    print(f"Overture release {release}", file=sys.stderr, flush=True)
    s3 = pafs.S3FileSystem(anonymous=True, region="us-west-2")
    data = ds.dataset(f"{BUCKET}/release/{release}/theme=places/type=place/", filesystem=s3, format="parquet")

    # Rough box around the 50 states; the address region does the real US filter.
    xmin, xmax, ymin, ymax = -179.5, -66.0, 18.5, 71.5
    flt = ((pc.field("bbox", "xmin") > xmin) & (pc.field("bbox", "xmax") < xmax)
           & (pc.field("bbox", "ymin") > ymin) & (pc.field("bbox", "ymax") < ymax)
           & pc.field("taxonomy", "primary").isin(list(CANDIDATE_TAX)))
    cols = ["id", "names", "taxonomy", "brand", "websites", "phones", "addresses", "bbox",
            "confidence", "operating_status"]

    branches = []
    seen = 0
    for batch in data.to_batches(columns=cols, filter=flt, batch_size=50_000):
        for r in batch.to_pylist():
            seen += 1
            if r.get("operating_status") and r["operating_status"] != "open":
                continue
            if (r.get("confidence") or 0) < 0.5:
                continue
            name = ((r.get("names") or {}).get("primary") or "").strip()
            if not name or not SUPPLIER.search(name) or NOT_SUPPLIER.search(name):
                continue
            addr = (r.get("addresses") or [{}])[0] or {}
            if addr.get("country") != "US":
                continue
            st = (addr.get("region") or "").upper()
            if st not in STATES:
                continue
            brand = (((r.get("brand") or {}).get("names") or {}).get("primary"))
            bb = r["bbox"]
            branches.append({
                "name": name,
                "brand": brand,
                "seg": segment(name),
                "st": st,
                "city": (addr.get("locality") or "").title() or None,
                "lat": round((bb["ymin"] + bb["ymax"]) / 2, 5),
                "lon": round((bb["xmin"] + bb["xmax"]) / 2, 5),
                "web": (r.get("websites") or [None])[0],
                "phone": (r.get("phones") or [None])[0],
            })
        print(f"  scanned {seen:,} candidates, kept {len(branches):,}", file=sys.stderr, flush=True)

    # Drop duplicates of the same place (same name within ~100 m).
    uniq = {}
    for b in branches:
        uniq.setdefault((norm(b["name"]), round(b["lat"], 3), round(b["lon"], 3)), b)
    branches = list(uniq.values())

    # Union-find: same brand, same cleaned name, or same website domain = one company.
    parent = list(range(len(branches)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first = {}
    for i, b in enumerate(branches):
        keys = []
        n = norm(b["name"])
        if n and n not in GENERIC:
            keys.append("n:" + n)
        if b["brand"]:
            keys.append("n:" + norm(b["brand"]))
        d = domain(b["web"])
        if d:
            keys.append("d:" + d)
        for k in keys:
            if k in first:
                parent[find(i)] = find(first[k])
            else:
                first[k] = i

    groups = {}
    for i, b in enumerate(branches):
        groups.setdefault(find(i), []).append(b)

    companies = []
    for members in groups.values():
        names = {}
        for m in members:
            label = m["brand"] or m["name"]
            names[label] = names.get(label, 0) + 1
        name = max(names.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
        segs = {}
        for m in members:
            segs[m["seg"]] = segs.get(m["seg"], 0) + 1
        chain = next((n for n, rx in NATIONAL_RE if any(rx.search(m["name"]) for m in members)), None)
        cust = next((CUSTOMER_KEYS[norm(m["name"])] for m in members if norm(m["name"]) in CUSTOMER_KEYS), None)
        web = next((m["web"] for m in members if domain(m["web"])), None)
        companies.append({
            "name": name,
            "seg": max(segs.items(), key=lambda kv: kv[1])[0],
            "chain": chain,
            "customer": cust,
            "web": web,
            "b": [[m["st"], m["city"], m["lat"], m["lon"], m["seg"], m["phone"]] for m in members],
        })
    companies.sort(key=lambda c: -len(c["b"]))

    coverage = {}
    for b in branches:
        coverage[b["st"]] = coverage.get(b["st"], 0) + 1

    if len(branches) < 5000:
        raise SystemExit(f"only {len(branches)} branches, keeping the previous data")

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "release": release,
        "source": "Overture Maps Foundation, Places theme (CDLA Permissive 2.0 / ODbL)",
        "branches": len(branches),
        "coverage": coverage,
        "companies": companies,
    }
    path = Path(__file__).resolve().parent.parent / "data" / "accounts.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    print(f"wrote {len(companies):,} companies from {len(branches):,} branches", file=sys.stderr)


if __name__ == "__main__":
    main()
