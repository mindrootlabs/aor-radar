# AOR Radar

Daily scan for AOR (Supreme Court of India) retainer, panel and empanelment openings.
A Mindroot Labs project. Runs free on GitHub Actions, dashboard on GitHub Pages.

## How it works
GitHub Actions (07:30 IST daily) -> `collector/main.py` -> fetches RSS + web search ->
Claude filters/extracts real openings -> `docs/openings.json` -> email digest of NEW items.
`docs/index.html` is the dashboard (Open / Apply links, filters, mark applied/ignore).

## Setup (about 15 minutes)
1. Create a repo under the Mindroot Labs GitHub account, push this folder to it.
2. **Settings > Pages**: Source = "Deploy from a branch", branch `main`, folder `/docs`. Note the URL.
3. **Settings > Secrets and variables > Actions**, add secrets:
   - `ANTHROPIC_API_KEY` (required)
   - `BRAVE_API_KEY` (strongly recommended; free tier at brave.com/search/api)
   - Email: `SMTP_USER` (Gmail address that sends), `SMTP_PASS` (Gmail *App Password*, needs 2-step verification on), `EMAIL_TO` (his email, comma-separate for several)
   - Variable (not secret): `DASHBOARD_URL` = the Pages URL, so the digest links to it
4. **Actions tab > Daily AOR scan > Run workflow** to test now.
5. Check the first runs' logs. Web search (Brave) is the main source; RSS currently has only Bar & Bench, add more feeds in `config/sources.yaml` as you find them.

## Tuning
Edit `config/sources.yaml` (search queries, feeds). The classifier prompt is in `collector/main.py`.
Applied/ignored marks are stored in his browser only.

## Local test
```
pip install -r requirements.txt
ANTHROPIC_API_KEY=... BRAVE_API_KEY=... python collector/main.py --dry-run
```
