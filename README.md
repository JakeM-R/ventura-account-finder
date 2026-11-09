# Account finder for Ventura

Pick a segment and a state. Get the building-materials suppliers worth calling, biggest first.

Live: https://jakem-r.github.io/ventura-account-finder/

- `scripts/fetch_accounts.py` pulls supplier locations from Overture Maps Places (the open business listings dataset) for every US state, sorts them into roofing, gypsum and drywall, lumber and building supplies, and groups branches into companies by brand, cleaned-up name and website domain.
- `.github/workflows/refresh.yml` runs it every Monday and commits `data/accounts.json`.
- `index.html` is the whole front end: a static page with Leaflet, no build step, no backend, no model calls.

Branch counts only cover listed places, so treat them as a lower bound. Places data © Overture Maps Foundation (CDLA Permissive 2.0 / ODbL).
