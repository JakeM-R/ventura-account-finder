#!/usr/bin/env python3
"""Pull building-materials suppliers from OpenStreetMap and group branches into companies.

Runs weekly in GitHub Actions. Writes data/accounts.json. Standard library only.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

# Supplier tags worth looking at. Everything is then classified into a segment or dropped.
QUERY = """
[out:json][timeout:90];
area["ISO3166-2"="US-{st}"]->.a;
(
  nwr["shop"="trade"](area.a);
  nwr["craft"="roofer"]["shop"](area.a);
  nwr["shop"="doityourself"]["name"~"Lumber|Building Supply|Building Materials",i](area.a);
);
out tags center;
"""

SEGMENT_TRADE = {
    "roofing": "roofing", "roofing_supplies": "roofing", "siding": "roofing",
    "drywall": "gypsum", "gypsum": "gypsum", "insulation": "gypsum", "plaster": "gypsum",
    "lumber": "lumber", "timber": "lumber", "wood": "lumber", "sawmill": "lumber",
    "building_supplies": "building", "building_materials": "building", "builder": "building",
    "brick": "building", "masonry": "building", "concrete": "building", "stone": "building",
}
SEGMENT_NAME = [
    ("roofing", re.compile(r"\broof|\bsiding\b|shingle", re.I)),
    ("gypsum", re.compile(r"gypsum|drywall|wallboard|insulation|acoustic", re.I)),
    ("lumber", re.compile(r"lumber|timber|millwork|truss", re.I)),
    ("building", re.compile(r"building (supply|supplies|materials|products|center)|builders? supply|masonry|brick|block", re.I)),
]
# Trades that are clearly not this ICP.
EXCLUDE_TRADE = {"plumbing", "electrical", "hvac", "agricultural", "garden_centre", "tiles",
                 "bathroom", "kitchen", "paint", "fire_protection", "pest_control", "pool",
                 "car_parts", "tools", "welding", "fasteners", "signs", "glass", "doors", "windows"}
EXCLUDE_NAME = re.compile(r"plumb|electric|hvac|heating|cooling|pool|tractor|auto parts|tire", re.I)

NATIONAL_CHAINS = [
    ("ABC Supply", r"\babc supply"), ("Beacon", r"\bbeacon\b|\bqxo\b|allied building products"),
    ("SRS Distribution", r"\bsrs\b"), ("The Home Depot", r"home depot"), ("Lowe's", r"\blowe'?s\b"),
    ("Menards", r"menards"), ("84 Lumber", r"\b84 lumber"), ("Builders FirstSource", r"builders firstsource|\bbmc\b"),
    ("L&W Supply", r"l ?& ?w supply"), ("GMS", r"\bgms\b|gypsum management"),
    ("Foundation Building Materials", r"foundation building materials|\bfbm\b"), ("Ferguson", r"ferguson"),
    ("US LBM", r"\bus lbm\b"), ("Ace Hardware", r"\bace hardware"), ("True Value", r"true value"),
    ("Do it Best", r"do it best"), ("Tractor Supply", r"tractor supply"),
]
NATIONAL_RE = [(n, re.compile(p, re.I)) for n, p in NATIONAL_CHAINS]

# From the customer logo wall on ventura.ai (checked 2026-11-09).
VENTURA_CUSTOMERS = [
    "Action Gypsum", "Bone Roofing Supply", "Dairyman's Supply", "Gemco Roofing & Building Supply",
    "Heely Brown", "Industrial Metal Supply", "Lakeside Roofing & Siding Materials",
    "New Castle Building Products", "Richards Building Supply", "Roofers Mart", "SPEC Building Materials",
]

SUFFIX = re.compile(r"\b(inc|llc|l\.l\.c|co|corp|corporation|company|ltd|lp|the)\b\.?", re.I)


def norm(name):
    s = name.lower().replace("’", "'").replace("&", " and ")
    s = re.sub(r"\s+[-–|@]\s+.*$", "", s)          # "Acme Lumber - Columbus"
    s = re.sub(r"\(.*?\)", "", s)
    s = SUFFIX.sub("", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


CUSTOMER_KEYS = {norm(c): c for c in VENTURA_CUSTOMERS}


def domain(url):
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    host = urllib.parse.urlparse(url).netloc.lower()
    host = host.split(":")[0]
    host = re.sub(r"^www\d?\.", "", host)
    if not host or host in {"facebook.com", "google.com", "yelp.com", "business.site", "linkedin.com"}:
        return None
    return host


def classify(tags):
    trade = (tags.get("trade") or "").lower()
    name = tags.get("name") or tags.get("brand") or ""
    if trade in EXCLUDE_TRADE:
        return None
    for t in re.split(r"[;,]\s*", trade):
        if t in SEGMENT_TRADE:
            return SEGMENT_TRADE[t]
    if EXCLUDE_NAME.search(name):
        return None
    if tags.get("craft") == "roofer":
        return "roofing"
    for seg, rx in SEGMENT_NAME:
        if rx.search(name):
            return seg
    return None


def overpass(query):
    body = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for attempt in range(2):
        for url in MIRRORS:
            try:
                req = urllib.request.Request(url, data=body, headers={"User-Agent": "ventura-account-finder/1.0"})
                with urllib.request.urlopen(req, timeout=100) as r:
                    return json.load(r), url
            except Exception as e:  # noqa: BLE001
                last = e
                print(f"  {url} failed: {e}", file=sys.stderr)
                time.sleep(5)
        time.sleep(20)
    raise RuntimeError(f"all mirrors failed: {last}")


def main():
    branches = []
    coverage = {}
    failed = []
    for st in STATES:
        print(f"{st}...", file=sys.stderr, flush=True)
        try:
            data, src = overpass(QUERY.format(st=st))
        except RuntimeError as e:
            print(f"  skipping {st}: {e}", file=sys.stderr)
            failed.append(st)
            continue
        kept = 0
        seen = set()
        for e in data.get("elements", []):
            key = (e["type"], e["id"])
            if key in seen:
                continue
            seen.add(key)
            t = e.get("tags", {})
            name = t.get("brand") or t.get("operator") if not t.get("name") else t.get("name")
            if not name:
                continue
            seg = classify(t)
            if not seg:
                continue
            lat = e.get("lat") or (e.get("center") or {}).get("lat")
            lon = e.get("lon") or (e.get("center") or {}).get("lon")
            if lat is None:
                continue
            branches.append({
                "id": f"{e['type'][0]}{e['id']}",
                "name": name,
                "brand": t.get("brand") or t.get("operator"),
                "seg": seg,
                "st": st,
                "city": t.get("addr:city"),
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "web": t.get("website") or t.get("contact:website") or t.get("brand:website"),
                "phone": t.get("phone") or t.get("contact:phone"),
            })
            kept += 1
        coverage[st] = kept
        time.sleep(3)

    # Union-find: same brand/operator, same normalised name, or same website domain = one company.
    parent = list(range(len(branches)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        parent[find(a)] = find(b)

    first = {}
    for i, b in enumerate(branches):
        keys = ["n:" + norm(b["name"])]
        if b["brand"]:
            keys.append("n:" + norm(b["brand"]))
        d = domain(b["web"])
        if d:
            keys.append("d:" + d)
        for k in keys:
            if k in first:
                union(i, first[k])
            else:
                first[k] = i

    groups = {}
    for i, b in enumerate(branches):
        groups.setdefault(find(i), []).append(b)

    companies = []
    for members in groups.values():
        names = {}
        for m in members:
            names[m["brand"] or m["name"]] = names.get(m["brand"] or m["name"], 0) + 1
        name = max(names.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
        segs = {}
        for m in members:
            segs[m["seg"]] = segs.get(m["seg"], 0) + 1
        chain = next((n for n, rx in NATIONAL_RE if rx.search(name)), None)
        cust = CUSTOMER_KEYS.get(norm(name))
        web = next((m["web"] for m in members if m["web"]), None)
        companies.append({
            "name": name,
            "seg": max(segs.items(), key=lambda kv: kv[1])[0],
            "chain": chain,
            "customer": cust,
            "web": web,
            "b": [[m["st"], m["city"], m["lat"], m["lon"], m["seg"], m["phone"]] for m in members],
        })
    companies.sort(key=lambda c: -len(c["b"]))

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": "OpenStreetMap contributors, via the Overpass API (ODbL)",
        "branches": len(branches),
        "coverage": coverage,
        "failed": failed,
        "companies": companies,
    }
    if len(failed) > 10:
        raise SystemExit(f"too many states failed ({len(failed)}), keeping the previous data")
    path = Path(__file__).resolve().parent.parent / "data" / "accounts.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    print(f"wrote {len(companies)} companies from {len(branches)} branches", file=sys.stderr)


if __name__ == "__main__":
    main()
