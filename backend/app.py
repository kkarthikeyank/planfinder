"""
Flask API wrapping the existing validate_ma_plan.py validator.

Endpoints:
  GET  /api/contracts                 -> the 4 selectable contracts (org, contract, index_url), read
                                          straight from validate_ma_plan.PLANS -- nothing hardcoded here.
  POST /api/jobs   {contract: "H9207"} -> starts a background validation job, returns {job_id}
  GET  /api/jobs/<job_id>              -> job status + summary + file list once complete
  GET  /api/jobs/<job_id>/files/<name> -> download one generated CSV

Each job runs validate_ma_plan.process_plan() (unmodified business logic) in its own
working directory, then reports back exactly which CSVs it produced.
"""
import os
import re
import threading
import traceback
import uuid
from collections import defaultdict

from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS

import validate_ma_plan as validator

app = Flask(__name__)
CORS(app)

JOBS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

# job_id -> {status, org, contract, started_at, finished_at, summary, files, error}
JOBS = {}
JOBS_LOCK = threading.Lock()

# The 4 contracts this tool offers, read directly from the existing script's
# PLANS configuration -- never hand-typed here, so this can never drift out
# of sync with validate_ma_plan.py.
CONTRACTS = [{"org": org, "contract": contract, "index_url": url} for org, contract, url in validator.PLANS]
CONTRACTS_BY_ID = {c["contract"]: c for c in CONTRACTS}

CODE_RE = re.compile(r"\[([A-Z]\d{4})\]")


def _summarize_appendix_e_coverage(cov_path):
    """Reads the Appendix E coverage CSV process_plan()/write_appendix_e_coverage()
    already produces and groups it by error code, for the UI: which resource
    type(s) each code was tested against, total pass/fail counts, a general
    description of what the check verifies, and up to 2 real failing examples
    (identifier + expected vs actual) so a FAIL isn't just a bare number."""
    import csv as csv_mod

    codes = {}
    with open(cov_path, newline="", encoding="utf-8") as f:
        for row in csv_mod.DictReader(f):
            code = row["error_code"]
            entry = codes.get(code)
            if entry is None:
                entry = {
                    "code": code,
                    "level": row.get("level"),
                    "name": row.get("bug_title"),
                    "status": row.get("status"),
                    "expected_description": validator.expected_for(code),
                    "pass_count": int(row.get("pass_count") or 0),
                    "fail_count": int(row.get("fail_count") or 0),
                    "total_records_tested": row.get("total_records_tested") or "0",
                    "failing_record_count": row.get("failing_record_count") or "0",
                    "resource_types": set(),
                    "examples": [],
                    "note": row.get("note") or "",
                }
                codes[code] = entry
            for t in (row.get("resource_type") or "").split(","):
                t = t.strip()
                if t:
                    entry["resource_types"].add(t)
            if row.get("identifier") and len(entry["examples"]) < 2:
                entry["examples"].append({
                    "org": row.get("org"),
                    "contract": row.get("contract"),
                    "resource_type": row.get("resource_type"),
                    "identifier": row.get("identifier"),
                    "smile_id": row.get("smile_id"),
                    "expected": row.get("expected"),
                    "actual": row.get("actual"),
                })

    result = []
    for code, entry in sorted(codes.items()):
        entry["resource_types"] = sorted(entry["resource_types"])
        entry["resource_type_count"] = len(entry["resource_types"])
        result.append(entry)
    return result


def _run_job(job_id, org, contract, index_url):
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    cwd_before = os.getcwd()
    try:
        os.chdir(job_dir)

        rows = [["Organization", "Contract", "File Role", "URL", "HTTP Status", "HEAD Supported",
                  "Conditional GET (304)", "Content-Type OK", "ETag Present", "Last-Modified Present",
                  "Resource Info", "Check", "Result", "Detail"]]
        global_code_examples = defaultdict(list)

        result = validator.process_plan(org, contract, index_url, rows, global_code_examples)
        bundles, mr_providers = result if result else ({}, [])

        out_path = f"ma_directory_validation_report_{contract}.csv"
        import csv as csv_mod
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            csv_mod.writer(f).writerows(rows)

        cov_path = validator.write_appendix_e_coverage(rows, out_path, global_code_examples)
        ab_file = validator.write_appendix_b_summary(cov_path, contract)

        fails = [r for r in rows[1:] if r[12] == "FAIL"]
        passes = [r for r in rows[1:] if r[12] == "PASS"]

        total_records = sum(len(v) for v in bundles.values()) + len(mr_providers)

        # Distinct records with at least one field issue, from the same
        # per-record companion CSV process_plan() already wrote
        # (missing_required_fields_<contract>.csv) -- read back rather than
        # recomputed, so this can never drift from the actual check results.
        failed_ids = set()
        mf_path = f"missing_required_fields_{contract}.csv"
        if os.path.exists(mf_path):
            with open(mf_path, newline="", encoding="utf-8") as f:
                for row in csv_mod.DictReader(f):
                    failed_ids.add((row.get("Resource Type"), row.get("Resource ID")))
        ph_path = f"placeholders_{contract}.csv"
        if os.path.exists(ph_path):
            with open(ph_path, newline="", encoding="utf-8") as f:
                for row in csv_mod.DictReader(f):
                    failed_ids.add((row.get("Resource Type"), row.get("Resource ID")))
        failed_records = len(failed_ids)
        valid_records = max(0, total_records - failed_records)

        summary = {
            "org": org,
            "contract": contract,
            "index_url": index_url,
            "total_records_processed": total_records,
            "valid_records": valid_records,
            "failed_records": failed_records,
            "checks_passed": len(passes),
            "checks_failed": len(fails),
            "resource_counts": {rtype: len(entries) for rtype, entries in bundles.items()},
            "machine_readable_providers": len(mr_providers),
        }

        files = sorted(f for f in os.listdir(job_dir) if f.lower().endswith(".csv"))
        summary["csv_files_generated"] = len(files)

        code_summary = _summarize_appendix_e_coverage(cov_path)
        summary["codes_tested"] = sum(1 for c in code_summary if c["status"] in ("FAIL_SEEN", "PASS_ONLY"))
        summary["codes_total"] = len(code_summary)

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "completed"
            JOBS[job_id]["summary"] = summary
            JOBS[job_id]["files"] = files
            JOBS[job_id]["codes"] = code_summary
    except Exception as e:
        traceback.print_exc()
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = str(e)
    finally:
        os.chdir(cwd_before)
        with JOBS_LOCK:
            JOBS[job_id]["finished"] = True


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/contracts")
def list_contracts():
    return jsonify({"contracts": CONTRACTS})


@app.post("/api/jobs")
def create_job():
    data = request.get_json(silent=True) or {}
    contract = (data.get("contract") or "").strip().upper()
    plan = CONTRACTS_BY_ID.get(contract)
    if not plan:
        return jsonify({"error": f"Unknown contract '{contract}'. Valid options: "
                                  f"{', '.join(CONTRACTS_BY_ID.keys())}"}), 400

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "org": plan["org"],
            "contract": plan["contract"],
            "summary": None,
            "files": [],
            "codes": [],
            "error": None,
        }

    t = threading.Thread(target=_run_job, args=(job_id, plan["org"], plan["contract"], plan["index_url"]), daemon=True)
    t.start()
    return jsonify({"job_id": job_id}), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    # Never leak internal exception detail beyond a friendly message.
    public_error = "Processing failed. Please try again." if job.get("error") else None
    return jsonify({
        "job_id": job["job_id"],
        "status": job["status"],
        "org": job["org"],
        "contract": job["contract"],
        "summary": job["summary"],
        "files": job["files"],
        "error": public_error,
    })


@app.get("/api/jobs/<job_id>/codes")
def job_codes(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "completed":
        return jsonify({"error": "Job is not completed yet"}), 409
    return jsonify({"codes": job.get("codes", [])})


@app.get("/api/jobs/<job_id>/files/<path:filename>")
def download_file(job_id, filename):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    job_dir = os.path.join(JOBS_DIR, job_id)
    if filename not in job.get("files", []):
        abort(404)
    return send_from_directory(job_dir, filename, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
