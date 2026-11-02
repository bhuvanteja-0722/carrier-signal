# Career Signal — Live Career Market Agent

Search a role and location to find current postings across 200 employer-hosted career boards, browse the expanded A–Z directory of those employers plus 80 curated multinational career sites, compare up to three live-connected companies, review employer-description themes and get a project-based learning plan.

Company cards include hiring locations for the current search, LinkedIn company search, and location-aware LinkedIn job search links. These open LinkedIn search results so users can select the correct official company page or listing.

The directory includes major multinational employers such as Visa, Mastercard, Microsoft, Google, Amazon, IBM, Accenture, Deloitte, TCS, Toyota, Siemens, Unilever and Pfizer. These additional entries link to official career pages and are explicitly marked as career-site-only when their job system is not live-connected. The 200-board live count continues to describe only feeds the app actually reads.

## Run the app

Requires Python 3.10+. From this folder:

```sh
python3 app.py
```

Open http://localhost:8000. The first search checks the public Greenhouse, Lever and Ashby job feeds in the employer registry. Results link to the employer's hosted posting. Feeds are cached for 15 minutes; **Refresh data** bypasses the cache.

## Deploy free on Render

1. Push this repository to GitHub.
2. In Render, choose **New → Blueprint**, connect the repository, and deploy the included `render.yaml`.
3. Use the generated `https://...onrender.com` URL. The free instance sleeps after inactivity and may take a short time to wake when opened.

The free blueprint intentionally has no paid persistent disk. The SQLite account database and saved market history live on the temporary instance filesystem, so they can be cleared when Render restarts, sleeps, or redeploys the service. Do not rely on free-tier accounts for durable storage. To retain account data, move to a paid service with a persistent disk or an external database.

No API key is needed for these public listings. Live openings vary by role, location and employer. A board with no matching job may still be a reachable career page. `/api/sources` lists all configured boards and their hosted career URLs.

## Trends

The app stores aggregate daily snapshots by role, location and country in `market_history.json`. It compares availability and skill mentions with a prior saved snapshot after at least 12 hours. A first search creates the baseline, so historical direction appears only after later searches.

## MCP server

The app exposes these read-only tools over stdio:

- `search_jobs` — find current postings across the employer boards.
- `analyze_role_market` — return job signals, skill gaps and a learning path.
- `list_employer_sources` — list the 200 configured career boards.

Launch with `python3 /absolute/path/to/app.py --mcp` in an MCP host. The Playwright MCP connection in Codex is used to operate and inspect the browser; the app's job data comes from the employers' public ATS feeds.

## Data source and limits

The board catalog is a 200-company subset of the [LastRound AI ATS Company Directory](https://github.com/fyrosofttech/lastroundai-hiring-data/tree/main/ats-directory), licensed CC BY 4.0. Catalog entries were independently checked against their public feeds on 2 November 2026. The feed APIs may change or become unavailable; the UI reports how many boards were reachable on each search.

Current skill frequency is a snapshot of returned postings, not proof of long-term demand. The app compares saved snapshots for a directional signal. Listed pay is shown only when a board includes salary fields; currencies are kept with the source data where available.

The role briefing extracts common responsibility sentences from live descriptions. Pay estimates appear only when enough employer-disclosed salary ranges are available; company cards identify company-specific disclosed pay separately from role-market estimates. Layoff links open a current news search and do not state or verify a layoff status. Promotion paths are broad career guidance; company title levels are inferred from current openings and do not establish internal promotion routes or timelines.

## Accounts and saved data

Create an account from the profile button to save a target role, location, country, skills, and up to three company comparisons. Data is stored in `career_signal_accounts.sqlite3`. Passwords use salted PBKDF2 hashes; login sessions use HttpOnly, SameSite cookies. This version does not yet provide email verification, password reset, or hosted multi-device sync. On free Render, the database is temporary and may be cleared when the instance restarts or redeploys.

## Files

- `app.py` — local web server, ATS feed readers, market analysis, daily snapshots and MCP server.
- `employer_sources.json` — 200 public employer board records and source attribution.
- `major_employers.json` — curated multinational employer career pages (links only until their job feeds are integrated).
- `index.html`, `style.css`, `app.js` — responsive interface.
