# Provider Directory Validation Tool

A simple internal web app around the existing `validate_ma_plan.py` MA provider
directory validator. A user picks one of the 4 Contract IDs, the existing
Python validation logic runs against that contract's live CMS index URL, and
the app returns a summary plus every CSV report the script already produces.

## Architecture

```
Netlify (static frontend)  --HTTPS-->  Python backend (Flask API)  --HTTPS-->  CMS provider directory endpoints
     frontend/                              backend/
```

- **`backend/`** — Flask API wrapping `validate_ma_plan.py` **unchanged** (the
  original script's business/validation logic was not rewritten; `app.py`
  only calls into it and reports back what it produced). Runs each contract
  as a background job because a full run (large FHIR files, tens of MB) can
  take several minutes — far longer than Netlify Functions allow.
- **`frontend/`** — static HTML/CSS/JS. Deployed to Netlify. Talks to the
  backend over HTTPS, polling for job status.

The 4 selectable Contract IDs (`H1619`, `H3124`, `H9207` under org `JHP`, and
`H5826` under org `CHPW`) are read directly from `validate_ma_plan.PLANS` —
they are never hand-typed in the frontend, so the UI can't drift out of sync
with the script's configuration.

## User flow

1. Open the site → see 4 Contract ID cards.
2. Click one → confirm screen → **Start Processing**.
3. Frontend calls `POST /api/jobs`, gets a `job_id`, and polls
   `GET /api/jobs/<job_id>` every few seconds.
4. Backend runs `validate_ma_plan.process_plan()` for that contract in a
   background thread, writing all of its usual CSV reports into a
   job-specific folder.
5. When done, the frontend shows a summary (records processed, valid/failed,
   checks passed/failed, CSV file count) and a download button per CSV.
6. **← Back to Contract Selection** returns to step 1.

If processing fails, the user sees a plain-English "Processing Failed"
message (no stack trace) with **Try Again** / **Back to Contract Selection**.

## Running locally

### Backend

```bash
cd backend
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
python app.py                # runs on http://localhost:5000
```

### Frontend

Edit `frontend/config.js` and point `API_BASE_URL` at `http://localhost:5000`
for local testing, then just open `frontend/index.html` in a browser, or
serve the folder:

```bash
cd frontend
python -m http.server 8080
```

## Deploying the backend (Python)

Netlify cannot run this backend — the validator downloads and checks 200MB+
files over several minutes, well past Netlify Functions' execution limits.
Deploy `backend/` to any always-on Python host. Render is the easiest:

1. Push this repo to GitHub.
2. On [render.com](https://render.com): **New +** → **Web Service** → connect
   the repo, root directory `backend/`.
3. Render will auto-detect `render.yaml` (build: `pip install -r
   requirements.txt`; start: `gunicorn app:app --workers 2 --threads 4
   --timeout 900 --bind 0.0.0.0:$PORT`). Otherwise set those manually.
4. Deploy. Note the resulting URL, e.g. `https://ma-directory-validator-api.onrender.com`.

(Railway or Fly.io work the same way — just point the build/start commands at
`backend/requirements.txt` and `gunicorn app:app`.)

## Deploying the frontend (Netlify)

1. Edit `frontend/config.js` and set `API_BASE_URL` to your deployed backend
   URL from the step above (no trailing slash).
2. Push to GitHub.
3. On [app.netlify.com](https://app.netlify.com): **Add new site** → **Import
   an existing project** → connect the repo.
4. Netlify reads `netlify.toml` at the repo root, which publishes the
   `frontend/` folder directly (no build step — it's static HTML/CSS/JS).
5. Deploy. Open the resulting `*.netlify.app` URL — that's the one link your
   users need.

## What the backend does NOT change in the original script

- All Appendix A/B/D/E validation rules, error codes, thresholds (NPI format,
  phone format, ZIP format, freshness windows, etc.) are untouched.
- All companion CSV reports (`missing_required_fields_*.csv`,
  `phone_number_issues_*.csv`, `code_<CODE>_*.csv`, the Appendix E coverage
  CSV, the Appendix B summary, etc.) are still produced exactly as before.
- `app.py` only adds: a per-job working directory (so concurrent runs on
  different contracts don't collide), a background thread + status polling
  wrapper, and a summary derived by reading the same report/companion CSVs
  the script already writes.

## Notes / known constraints

- `SKIP_NPPES_LOOKUP = True` in `validate_ma_plan.py` (unchanged from the
  original) skips the live NPPES registry check for speed. Flip it to
  `False` there if you want P1002/P1003/P1005 to run for real.
- Each backend job writes its CSVs under `backend/jobs/<job_id>/` and they
  stay there until you clean them up manually — add a periodic cleanup job
  if disk usage becomes a concern on your host.
- The in-memory job store (`JOBS` dict in `app.py`) is per-process. If your
  host restarts the backend process, in-flight/completed job records (and
  their download links) are lost — acceptable for an internal tool used
  interactively, but worth knowing.
