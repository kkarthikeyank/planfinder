#!/usr/bin/env python3
"""
MA Provider Directory Validator
--------------------------------
Validates CMS Medicare Plan Finder (MPF) MA provider directory index files and
their constituent JSON files against the CMS Technical Implementation Guide
(mpf_ma_provider_directory_technical_guide, Appendices A, B, and D).

Supports BOTH submission formats:
  - Machine-readable JSON (Appendix A): array-of-providers files
  - FHIR-based JSON (Appendix B): FHIR Bundles of Practitioner, PractitionerRole,
    Location, Organization, OrganizationAffiliation, InsurancePlan, etc.

Requires only the Python standard library (no pip installs needed).

USAGE
-----
1. Run all plans hardcoded in the PLANS list below:
       python validate_ma_directories.py

2. Run a single plan directly on the command line:
       python validate_ma_directories.py "CHPW" "H5826" "https://.../index.json"

3. Run a batch from a CSV file (no header row, one plan per line: ORG,CONTRACT,INDEX_URL):
       python validate_ma_directories.py plans.csv

OUTPUT
------
- ma_directory_validation_report[_<CONTRACT>].csv  (detailed, one row per check)
- A pass/fail summary printed to the console

CHECKS PERFORMED
-----------------
HTTP / Appendix D:
  - Index and constituent files return 200, correct Content-Type
  - HEAD method supported
  - Conditional GET (If-None-Match) returns 304
  - ETag / Last-Modified / Content-Length present
  - File freshness: Last-Modified not older than 30 days (guide requires updates
    at least every 30 days)

Field-level / Appendix A & B:
  - Required fields present per resource type (NPI, name, phone, language,
    location, specialty, network linkage, address completeness, etc.)
  - NPI format (must be exactly 10 digits)
  - Record-level freshness: FHIR meta.lastUpdated / lastUpdatedOn not older than
    30 days
  - Contract-year consistency: the year in the index URL path / index.json's
    contract_year field must match InsurancePlan.period (FHIR) or the plans[].year
    field (machine-readable) -- a mismatch means CMS will likely ingest this data
    under the wrong contract year
  - Dangling location references: flags PractitionerRole / OrganizationAffiliation
    entries that reference a Location record with an incomplete/empty address

Note: FHIR files for large plans can be tens of MB; this script downloads each
constituent file fully into memory to validate it. That is expected and safe on
any modern machine, but may take a minute or two per plan depending on size and
network speed.
"""

import csv
import hashlib
import json
import os
import re
import socket
import sys
import time
import ssl
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Appendix E error catalogue -- every check below is tagged with the CMS
# error code it corresponds to, so failures in the report/console are
# traceable straight back to the Technical Implementation Guide (page 31-37).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorDef:
    code: str
    level: int      # 1=Fatal, 2=Record-Level, 3=Advisory
    name: str


ERROR_CATALOG = {e.code: e for e in [
    # Level 1 - Fatal Errors
    ErrorDef("C4001", 1, "HTTPCommunicationsError"),
    ErrorDef("C4002", 1, "X509CertError"),
    ErrorDef("C4003", 1, "FileRetrievalError"),
    ErrorDef("C4004", 1, "InvalidJSONSyntax"),          # index file syntax error during crawl
    ErrorDef("C4011", 1, "MissingURL"),
    ErrorDef("C4012", 1, "MissingFileDownloadURL"),
    ErrorDef("C4013", 1, "InvalidIndexFileFormat"),
    ErrorDef("C4014", 1, "ImproperURL"),
    ErrorDef("C4015", 1, "InvalidPlanDataFile"),
    ErrorDef("C4016", 1, "InvalidDataFileMR"),
    ErrorDef("C4017", 1, "InvalidPlanDataFileFHIRResource"),
    ErrorDef("C4018", 1, "InvalidPlanDataFileFHIRBundle"),
    ErrorDef("N3006", 1, "OmittedContractID"),
    ErrorDef("N3015", 1, "InvalidJSONSyntax"),           # data file syntax error
    ErrorDef("P1017", 1, "NoProvidersFound"),

    # Level 2 - Record-Level Skip Errors
    ErrorDef("A2001", 2, "MissingProviderAddresses"),
    ErrorDef("A2008", 2, "InvalidAddress"),
    ErrorDef("F5001", 2, "MissingNetworkReference"),
    ErrorDef("F5002", 2, "MissingOrganizationReference"),
    ErrorDef("F5003", 2, "MissingPractitionerReference"),
    ErrorDef("F5004", 2, "MissingLocationReference"),
    ErrorDef("F5005", 2, "BrokenNetworkReference"),
    ErrorDef("F5006", 2, "BrokenOrganizationReference"),
    ErrorDef("F5007", 2, "BrokenPractitionerReference"),
    ErrorDef("F5008", 2, "BrokenLocationReference"),
    ErrorDef("F5009", 2, "MultipleNPIonResource"),
    ErrorDef("N3001", 2, "MissingPlanID"),
    ErrorDef("N3002", 2, "InvalidMAPlanID"),
    ErrorDef("N3003", 2, "PlanNotAssociated"),
    ErrorDef("N3004", 2, "MissingContractYear"),
    ErrorDef("N3005", 2, "InvalidContractYear"),
    ErrorDef("N3011", 2, "UnknownContractID"),
    ErrorDef("N3012", 2, "UnknownPlanID"),
    ErrorDef("N3013", 2, "UnknownSegmentID"),
    ErrorDef("N3014", 2, "MismatchContractID"),
    ErrorDef("P1001", 2, "MissingProviderNPI"),
    ErrorDef("P1002", 2, "UnknownProviderNPI"),
    ErrorDef("P1003", 2, "DeactivatedProviderNPI"),
    ErrorDef("P1016", 2, "ProviderNotAssociated"),

    # Level 3 - Advisory Warnings
    ErrorDef("P1004", 3, "InvalidProviderType"),
    ErrorDef("P1005", 3, "MismatchProviderType"),
    ErrorDef("P1006", 3, "MissingProviderFirstName"),
    ErrorDef("P1007", 3, "MissingProviderLastName"),
    ErrorDef("P1008", 3, "MissingFacilityName"),
    ErrorDef("P1009", 3, "MissingSpecialty"),
    ErrorDef("P1010", 3, "MissingSex"),
    ErrorDef("P1011", 3, "MissingLanguage"),
    ErrorDef("P1012", 3, "MissingAcceptingPatients"),
    ErrorDef("P1013", 3, "InvalidDateFormat"),
    ErrorDef("P1014", 3, "FutureDate"),
    ErrorDef("P1018", 3, "InvalidAcceptingPatientsType"),
    ErrorDef("A2002", 3, "MissingProviderCity"),
    ErrorDef("A2003", 3, "MissingProviderState"),
    ErrorDef("A2004", 3, "InvalidProviderState"),
    ErrorDef("A2005", 3, "MissingProviderZip"),
    ErrorDef("A2006", 3, "InvalidProviderZip"),
    ErrorDef("A2007", 3, "MissingProviderStreetAddresses"),
    ErrorDef("A2009", 3, "MissingProviderPhoneNumber"),
    ErrorDef("A2010", 3, "InvalidProviderPhoneNumber"),
    ErrorDef("N3007", 3, "OmittedSegmentID"),
    ErrorDef("N3008", 3, "OmittedPlanID"),
    ErrorDef("C4005", 3, "HeadRequestFailed"),
    ErrorDef("C4006", 3, "MissingLastModifiedHeader"),
    ErrorDef("C4007", 3, "MissingContentLengthHeader"),
    ErrorDef("C4008", 3, "MissingContentTypeHeader"),
    ErrorDef("C4009", 3, "MissingETagHeader"),
    ErrorDef("C4010", 3, "StaleDataWarning"),
]}

# Codes that genuinely require data this script cannot obtain on its own
# (a live HPMS plan-universe export, a live NPPES registry snapshot, or a
# geocoding service). Reported explicitly -- with what the check would
# actually verify, what external data it needs, and what risk is left
# uncovered as a result -- instead of being silently skipped or faked.
NOT_IMPLEMENTED_CODES = {
    # N3011/N3012/N3013 removed from this list: a fixed known-valid-ID
    # registry (KNOWN_VALID_PLAN_IDS_BY_ORG) is now supplied directly for
    # CHPW and JHP, so these run as real checks for those orgs. An org with
    # no registry entry still reports INFO/skipped rather than a false pass.
    # N3006/N3007/N3008 removed from this list: this script now checks the
    # ma-plan-id STRING ITSELF for a blank Contract/Plan/Segment component
    # (see _parse_ma_plan_id / the InsurancePlan block in validate_fhir_bundle).
    # What remains genuinely not implemented is the HPMS-list variant of these
    # codes -- confirming an ID that's entirely ABSENT from the submission was
    # nonetheless expected by HPMS. That still requires HPMS's expected-ID
    # export, which this script has no access to.
    "N3003": {
        "reason": "Requires HPMS's expected-plan list to know a plan received zero providers.",
        "what_it_would_check": "That every plan HPMS expects for this contract has at least one associated provider record in the submission.",
        "requires": "A live HPMS export of the plans expected for this contract, cross-referenced against InsurancePlan/provider linkage in the submission.",
        "risk_if_skipped": "A plan that HPMS expects but that received zero providers in this submission will not be flagged.",
    },
    "A2008": {
        "reason": "Requires a geocoding service to confirm the address resolves to a real location.",
        "what_it_would_check": "That each Location/Organization street address is a real, geocodable location (not just structurally present).",
        "requires": "A geocoding API (e.g. US Census Bureau geocoder, Smarty, Google Geocoding) to resolve address -> lat/long.",
        "risk_if_skipped": "A structurally complete but fabricated/nonexistent address (e.g. '123 Fake St') will pass every other address check in this script.",
    },
}

# ---------------------------------------------------------------------------
# NPPES registry lookup (P1002/P1003/P1005) -- public API, no auth required:
# https://npiregistry.cms.hhs.gov/api/?number=<npi>&version=2.1
#
# A contract can carry tens of thousands of UNIQUE NPIs (H1619 alone has
# ~32,000 across Practitioner+Organization). Looking up every single one
# against a rate-limited public API would take a very long time. Instead:
# dedupe NPIs first, then check up to NPI_LOOKUP_MAX of them, and say so
# explicitly in the finding detail -- a partial check that's honest about
# its coverage beats either skipping this entirely or silently sampling.
# ---------------------------------------------------------------------------
NPPES_BASE_URL = "https://npiregistry.cms.hhs.gov/api/"
NPI_LOOKUP_MAX = 30
NPI_LOOKUP_TIMEOUT = 8
_NPI_LOOKUP_CACHE = {}
# Set to True to skip the NPPES registry lookup (P1002/P1003/P1005) entirely --
# it's the slowest part of a run (up to 300 sequential external API calls) and
# not needed for a quick check of the other 45+ Appendix E codes. Toggle back
# to False for a full run that includes registry verification.
SKIP_NPPES_LOOKUP = True


def nppes_lookup(npi):
    """Returns a dict {found, status, enumeration_type} or None if the
    lookup itself failed (network/timeout) -- None means 'couldn't check',
    NOT 'not found', so callers must not treat None as a P1002 finding."""
    if npi in _NPI_LOOKUP_CACHE:
        return _NPI_LOOKUP_CACHE[npi]
    try:
        url = f"{NPPES_BASE_URL}?number={npi}&version=2.1"
        req = urllib.request.Request(url, headers={"User-Agent": "MA-Directory-Validator/1.0"})
        with urllib.request.urlopen(req, timeout=NPI_LOOKUP_TIMEOUT, context=CTX) as resp:
            data = json.loads(resp.read())
        if data.get("result_count", 0) == 0 or not data.get("results"):
            result = {"found": False, "status": None, "enumeration_type": None}
        else:
            basic = data["results"][0].get("basic", {}) or {}
            result = {
                "found": True,
                "status": basic.get("status"),               # "A" active, "D" deactivated
                "enumeration_type": data["results"][0].get("enumeration_type"),  # "NPI-1" individual, "NPI-2" org
            }
    except Exception:
        result = None
    _NPI_LOOKUP_CACHE[npi] = result
    return result


def check_npi_registry(npi_to_submitted_type):
    """npi_to_submitted_type: {npi: "Individual"|"Facility"}. Runs P1002/P1003/P1005
    against up to NPI_LOOKUP_MAX unique NPIs and returns (rows, checked_count, total_unique)."""
    unique_npis = list(npi_to_submitted_type.keys())
    total_unique = len(unique_npis)
    sample = unique_npis[:NPI_LOOKUP_MAX]

    unknown = Bucket(); deactivated = Bucket(); type_mismatch = Bucket()
    checked = 0
    for npi in sample:
        result = nppes_lookup(npi)
        if result is None:
            continue  # lookup failed (network) -- don't count as a finding either way
        checked += 1
        tag = f"NPI:{npi}"
        if not result["found"]:
            unknown.hit(tag)
            continue
        else:
            unknown.pass_hit(tag)
        if result["status"] == "D":
            deactivated.hit(tag)
        else:
            deactivated.pass_hit(tag)
        submitted = npi_to_submitted_type.get(npi)
        registry_type = "Individual" if result["enumeration_type"] == "NPI-1" else "Facility"
        if submitted and submitted != registry_type:
            type_mismatch.hit(f"{tag} submitted={submitted} registry={registry_type}")
        else:
            type_mismatch.pass_hit(f"{tag} submitted={submitted} registry={registry_type}")

    coverage_note = (f" [sampled {checked} of {total_unique} unique NPIs in this file"
                      f"{' (capped at ' + str(NPI_LOOKUP_MAX) + ')' if total_unique > NPI_LOOKUP_MAX else ''}]")

    rows = [
        (code_row_suffix("P1002"), "PASS" if not unknown.count else "FAIL",
         f"{unknown.count} of {checked} checked NPIs are not found in NPPES{unknown.suffix()}{coverage_note}"),
        (code_row_suffix("P1003"), "PASS" if not deactivated.count else "FAIL",
         f"{deactivated.count} of {checked} checked NPIs are deactivated in NPPES{deactivated.suffix()}{coverage_note}"),
        (code_row_suffix("P1005"), "PASS" if not type_mismatch.count else "FAIL",
         f"{type_mismatch.count} of {checked} checked NPIs have a submitted type not matching NPPES{type_mismatch.suffix()}{coverage_note}"),
    ]
    return rows, checked, total_unique


# Appendix E, page 30 -- "CMS has defined three levels of findings" (verbatim
# from the Technical Implementation Guide).
LEVEL_DESCRIPTIONS = {
    1: ("Fatal Errors",
        "A fatal error represents a structural failure in the submission that prevents CMS from reading the "
        "file. This usually occurs due to malformed file syntax or hosting issues. The system cannot parse "
        "the data, and the entire dataset will be rejected. Data included in a fatal submission will not be "
        "shown on MPF until the issue has been resolved."),
    2: ("Record-Level Errors",
        "A record-level error occurs when a specific record contains invalid or incomplete data that "
        "prevents information from being displayed accurately. This error could apply to the entire "
        "provider record or only a part of the associated data (e.g., an address or specialty). After "
        "flagging a record-level error, CMS will continue processing the remainder of the dataset. Records "
        "flagged with this finding will not be shown on MPF until the issue has been resolved."),
    3: ("Informational Warnings",
        "An informational warning identifies a potential issue. These items will be reported as findings, "
        "but CMS will continue processing the dataset. Issues flagged with this finding will be shown on "
        "MPF, though MA organizations should review and remediate the issue as quickly as possible."),
}


def code_row_suffix(code):
    """Formats '<name> [<code>]' for embedding the Appendix E code into a
    check-name column without changing the CSV's column count."""
    e = ERROR_CATALOG.get(code)
    return f"{e.name} [{code}]" if e else code


# What each check is actually verifying -- printing "expected: PASS" for
# every failure was meaningless (every check's expected OUTCOME is trivially
# "PASS"). This is the real condition being tested, per Appendix E code, so
# the console's FAILING TEST CASES section can show a genuine expected-vs-
# actual pair instead of a placeholder. Kept in sync with webui/server.py's
# CODE_EXPECTED (same map, so the CLI and the web UI never show different
# text for the same code).
CODE_EXPECTED = {
    "C4001": "Endpoint is reachable and responds to HTTP requests",
    "C4002": "TLS certificate is valid, trusted, and not expired",
    "C4003": "Referenced file downloads/retrieves successfully (200 OK)",
    "C4004": "Index file is syntactically valid JSON",
    "C4005": "Endpoint supports the HEAD method",
    "C4006": "Response includes a Last-Modified header",
    "C4007": "Response includes a Content-Length header",
    "C4008": "Response includes a valid application/json Content-Type",
    "C4009": "Response includes an ETag header",
    "C4010": "File was last modified within the required freshness window",
    "C4011": "Required URL field is present and not blank",
    "C4012": "Index file lists at least one file-download URL",
    "C4013": "Index file follows the required top-level format",
    "C4014": "Each constituent URL is a single, well-formed https:// URL",
    "C4015": "Plan data file conforms to a recognized structure",
    "C4016": "Machine-readable file conforms to the Appendix A provider schema",
    "C4017": "FHIR resource is structurally valid",
    "C4018": "FHIR Bundle structure is valid (type, non-empty entries)",
    "N3001": "InsurancePlan carries a plan ID",
    "N3002": "MA plan ID matches the required H####-###-### format",
    "N3003": "Every HPMS-expected plan has at least one associated provider",
    "N3004": "InsurancePlan carries a contract year",
    "N3005": "Contract year in the data matches the hosting URL/index year",
    "N3006": "ma-plan-id's Contract ID component is not blank",
    "N3007": "ma-plan-id's Segment ID component is not blank",
    "N3008": "ma-plan-id's Plan ID component is not blank",
    "N3011": "Every contract ID referenced actually exists in HPMS",
    "N3012": "Every plan ID referenced actually exists in HPMS",
    "N3013": "Every segment ID referenced actually exists in HPMS",
    "N3014": "Plan ID's contract prefix matches the submitting contract",
    "N3015": "Constituent data file is syntactically valid JSON",
    "P1001": "Provider record carries an NPI",
    "P1002": "NPI exists in the NPPES registry",
    "P1003": "NPI is not deactivated in the NPPES registry",
    "P1004": "Facility carries a valid 'fac' type code under the correct system",
    "P1005": "Submitted provider type matches the NPPES registry type",
    "P1006": "Practitioner has a first (given) name",
    "P1007": "Practitioner has a last (family) name",
    "P1008": "Facility has a name",
    "P1009": "Record carries a specialty/taxonomy code",
    "P1010": "Practitioner has a gender/sex element",
    "P1011": "Practitioner has a language/communication element",
    "P1012": "Record carries an accepting-new-patients extension",
    "P1013": "Date fields use the required YYYY-MM-DD format",
    "P1014": "meta.lastUpdated/period is not a future-dated timestamp",
    "P1016": "Provider is associated with a valid plan",
    "P1017": "At least one provider-like record exists in the submission",
    "P1018": "Accepting-new-patients code is one of the valid values",
    "A2001": "Provider/facility has a resolvable address",
    "A2002": "Address includes a city",
    "A2003": "Address includes a state",
    "A2004": "State is a valid 2-letter code",
    "A2005": "Address includes a postal code",
    "A2006": "Postal code matches 5-digit or ZIP+4 format",
    "A2007": "Address includes a street line",
    "A2008": "Address resolves to a real, geocodable location",
    "A2009": "Provider/facility has a phone number",
    "A2010": "Phone number matches the required 10-digit format",
    "F5001": "Record carries a network reference",
    "F5002": "Record carries an organization reference",
    "F5003": "Record carries a practitioner reference",
    "F5004": "Record carries a location reference",
    "F5005": "Referenced network resolves to a real, included resource",
    "F5006": "Referenced organization resolves to a real, included resource",
    "F5007": "Referenced practitioner resolves to a real, included resource",
    "F5008": "Referenced location resolves to a real, included resource",
    "F5009": "Resource carries at most one NPI identifier",
}


def expected_for(code):
    if code in CODE_EXPECTED:
        return CODE_EXPECTED[code]
    edef = ERROR_CATALOG.get(code)
    name = edef.name if edef else code
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", name)
    return f"No '{spaced}' condition present"


# Codes that still fail their check (still counted, still written to every
# CSV, still contribute to the pass/fail totals) but are DISPLAYED as "WARN"
# instead of "FAIL" in console output -- a softer label for findings that
# are genuinely advisory rather than something broken. P1004 in particular:
# a facility missing the 'fac' type code is usually a data-modeling choice
# by the source system (e.g. a group practice coded 'prvgrp'), not a defect,
# so calling it a "bug" overstates it -- CMS itself classifies it Level 3
# (Informational Warning), not a blocking error.
WARNING_ONLY_CODES = {"P1004"}

# ---------------------------------------------------------------------------
# EDIT THIS if you don't want to pass arguments on the command line.
#
# To run plans SEPARATELY (one at a time) instead of all four together:
#   - Comment out (put a leading '#') on every row except the one you want,
#     then run:  python validate_ma_directories.py
#   - OR skip editing this file entirely and run one plan directly:
#       python validate_ma_directories.py "JHP" "H1619" "https://.../h1619/2027/index.json"
#     (this ignores the PLANS list below and writes a report scoped to just
#     that contract, e.g. ma_directory_validation_report_H1619.csv)
# ---------------------------------------------------------------------------
PLANS = [
    # org,      contract,  index_url
    ("JHP",   "H1619",  "https://medicare-advantage-plan-finder-provider-directory.jeffersonhealthplans.com/h1619/2027/index.json"),
    ("JHP",   "H3124",  "https://medicare-advantage-plan-finder-provider-directory.jeffersonhealthplans.com/h3124/2027/index.json"),
    ("JHP",   "H9207",  "https://medicare-advantage-plan-finder-provider-directory.jeffersonhealthplans.com/h9207/2027/index.json"),
    ("CHPW",  "H5826",  "https://medicare-advantage-plan-finder-provider-directory.interop.chpw.org/h5826/2027/index.json"),
]

TIMEOUT = 180  # some constituent files (e.g. Location) run 200MB+; 60s was too tight and produced false C4001s
NPI_SYS = "http://hl7.org/fhir/sid/us-npi"
CMS_PLAN_SYS = "http://cms.gov/medicare/ma-plan-id"
# P1016 (ProviderNotAssociated) -- confirms a PractitionerRole/
# OrganizationAffiliation carries at least one identifier under its own
# org's network-identifier system, i.e. it's actually tied to a real network
# rather than floating unassociated. Each submitting org hosts its network
# Organization under its own identifier system URL, so this is keyed by org,
# not one constant.
NETWORK_IDENTIFIER_SYSTEM_BY_ORG = {
    "JHP": "https://www.healthpartnersplans.com/fhir/plannet/network/identifier",
    "CHPW": "https://www.chpw.org/fhir/plannet/network/identifier",
}

# N3011/N3012/N3013 (Unknown Contract/Plan/Segment ID) -- these were
# previously NOT_IMPLEMENTED because they need HPMS's expected-ID universe,
# which this script has no API access to. This is that same universe, supplied
# directly as a fixed registry instead. Full ma-plan-id strings, not just
# contract IDs, because the same contract can have multiple valid plan IDs
# (e.g. CHPW's H5826 has both -014- and -017-) -- the CONTRACT/PLAN/SEGMENT
# breakdown below is derived from this list, not maintained separately.
KNOWN_VALID_PLAN_IDS_BY_ORG = {
    "CHPW": {"H5826-014-000", "H5826-017-000"},
    "JHP": {
        "H1619-001-000", "H1619-004-000",
        "H3124-003-000",
        "H9207-002-000", "H9207-004-000", "H9207-012-000", "H9207-013-000",
        "H9207-015-000", "H9207-016-000", "H9207-017-000", "H9207-018-000",
    },
}


def _known_id_registry(org):
    """Derives the known-valid Contract IDs and (Contract, Plan) pairs for
    org from KNOWN_VALID_PLAN_IDS_BY_ORG, for the N3011/N3012/N3013 checks
    below. Returns None if org has no registry configured (check skipped,
    not silently passed)."""
    full_ids = KNOWN_VALID_PLAN_IDS_BY_ORG.get(org)
    if not full_ids:
        return None
    contracts, plans = set(), set()
    for pid in full_ids:
        c, p, s = _parse_ma_plan_id(pid)
        contracts.add(c)
        plans.add((c, p))
    return {"contracts": contracts, "plans": plans, "full": full_ids}
ORGTYPE_SYS = "http://hl7.org/fhir/us/davinci-pdex-plan-net/CodeSystem/OrgTypeCS"
NPI_RE = re.compile(r"^\d{10}$")
MAPLANID_RE = re.compile(r"^[A-Z]\d{4}-\d{3}-\d{3}$")


def _parse_ma_plan_id(pid):
    """Splits an ma-plan-id ('H5826-014-000') into (contract_id, plan_id,
    segment_id). Any component that is missing or blank comes back as ''
    -- used to tell N3006/N3007/N3008 apart from a merely malformed ID
    (N3002): 'H5826--000' has a real, present contract+segment but an
    OMITTED plan ID specifically, which N3002 alone can't distinguish."""
    parts = [p.strip() for p in str(pid or "").split("-")]
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]
# A2006 rule (final, client-confirmed): Provider ZIP must be EXACTLY 5
# numeric digits -- nothing else. This supersedes the earlier lenient rule
# (which accepted a 4-digit base and ignored a ZIP+4 suffix). Now rejected:
# too short/long, non-numeric (letters, alphanumeric, dashes, spaces),
# ZIP+4 format, decimal/negative representations, blank/null, and the two
# placeholder values 00000/99999.
ZIP_RE = re.compile(r"^\d{5}$")
_ZIP_PLACEHOLDER_VALUES = {"00000", "99999"}


def zip_base_valid(zip_code):
    """A2006: valid ONLY if the value is exactly 5 numeric digits and not
    one of the known placeholder values (00000, 99999). Anything else --
    wrong length, non-digit characters, ZIP+4, decimal/negative forms --
    fails. (Presence -- i.e. blank/null -- is A2005's concern, checked
    separately before this function is ever called.)"""
    s = str(zip_code).strip() if zip_code is not None else ""
    if not ZIP_RE.match(s):
        return False
    return s not in _ZIP_PLACEHOLDER_VALUES
# Extracts the resource category from a constituent filename, e.g.
# "jhp-H9207-2027-practitionerrole-part1.json" -> "practitionerrole". Used to
# build the Resource File Processing Summary (one row per category: how many
# files, which files, how many records were indexed from them).
FILE_CATEGORY_RE = re.compile(r"-(\d{4})-([a-z]+)-part\d+\.json$", re.I)
# A2010 rule (final, client-confirmed): the RAW phone value must be exactly
# 10 digits, nothing else -- no stripping of formatting characters first.
# "(206) 555-1234", "206-555-1234", and "+1 2065551234" all now FAIL, since
# they contain non-digit characters; only a bare "2065551234" passes. This
# supersedes the earlier rule that stripped punctuation/spaces before
# checking length.
PHONE_RE = re.compile(r"^\d{10}$")
# A2004 rule (client-confirmed): Provider State must be a real USPS 2-letter
# state/territory/military code, uppercase, exactly 2 characters -- NOT just
# any 2 uppercase letters. A well-formed but non-existent code like "XX"
# must still fail, so format alone (regex) is insufficient; membership in
# the real code list is required too.
STATE_RE = re.compile(r"^[A-Z]{2}$")
US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "AS", "GU", "MP", "PR", "VI",
    "AA", "AE", "AP",
}


def state_valid(state):
    """A2004: valid ONLY if the value is exactly 2 uppercase letters AND a
    real USPS state/DC/territory/military code -- not just any 2-letter
    string (e.g. "XX" is format-valid but not a real code, so it fails)."""
    s = str(state).strip() if state is not None else ""
    if not STATE_RE.match(s):
        return False
    return s in US_STATE_CODES
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FRESHNESS_DAYS = 30
PLACEHOLDER_EXT_URL = "http://hapifhir.io/fhir/StructureDefinition/resource-placeholder"
# Organization has no native FHIR "qualification" element -- Da Vinci PlanNet
# adds facility taxonomy via this extension instead: a top-level extension
# with this URL, carrying a nested "code" sub-extension whose
# valueCodeableConcept.coding holds the actual taxonomy code. (Practitioner,
# unlike Organization, DOES have a native qualification[].code element in
# base FHIR -- that one is read directly, no extension involved.)
ORG_QUALIFICATION_EXT_URL = "http://hl7.org/fhir/us/davinci-pdex-plan-net/StructureDefinition/qualification"


def _org_qualification_codings(r):
    """Every taxonomy coding found under Organization's qualification
    extension (see ORG_QUALIFICATION_EXT_URL above). Empty list means no
    taxonomy present."""
    codings = []
    for ext in r.get("extension", []) or []:
        if ext.get("url") != ORG_QUALIFICATION_EXT_URL:
            continue
        for sub in ext.get("extension", []) or []:
            if sub.get("url") == "code":
                codings.extend((sub.get("valueCodeableConcept") or {}).get("coding", []) or [])
    return codings
CTX = ssl.create_default_context()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
# Local disk cache for downloaded GET bodies (index + constituent files).
# Disabled by default -- every run must hit the live endpoint and validate
# whatever it returns RIGHT NOW, not a possibly-stale copy from a prior run.
# Flip to True only for iterating on script changes against the same fixed
# data (e.g. re-testing report formatting) where re-downloading 200MB+ files
# repeatedly would otherwise slow down every edit-run cycle.
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "http_cache")
USE_CACHE = False


def _cache_paths(url):
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, key + ".body"), os.path.join(CACHE_DIR, key + ".meta.json")


def _cache_load(url):
    body_path, meta_path = _cache_paths(url)
    if not (os.path.exists(body_path) and os.path.exists(meta_path)):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        with open(body_path, "rb") as f:
            body = f.read()
        return meta["status"], meta["headers"], body
    except Exception:
        return None


def _cache_store(url, status, headers, body):
    os.makedirs(CACHE_DIR, exist_ok=True)
    body_path, meta_path = _cache_paths(url)
    try:
        with open(body_path, "wb") as f:
            f.write(body)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"status": status, "headers": headers}, f)
    except Exception:
        pass


def http_request(url, method="GET", headers=None, read_body=True):
    """Returns (status_code, headers_dict, body_bytes) -- never raises.
    read_body=False skips downloading the body (used for the conditional-GET
    304 check, so large files are not downloaded a second time).

    Some constituent files (e.g. Location) run 200MB+, so a plain socket
    timeout does NOT necessarily mean the server is down -- it can just mean
    the download didn't finish inside TIMEOUT seconds. The body always
    carries a clear reason string so a real DNS/refused/reset failure isn't
    confused with "still transferring, ran out of time".

    A plain GET-with-body is the only variant cached: HEAD and the
    conditional-GET (If-None-Match) probe exist specifically to test live
    HTTP behavior, so they always hit the network."""
    cacheable = USE_CACHE and method == "GET" and read_body and not (headers or {}).get("If-None-Match")
    if cacheable:
        cached = _cache_load(url)
        if cached is not None:
            return cached

    req = urllib.request.Request(url, method=method, headers=headers or {"User-Agent": "MA-Directory-Validator/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as resp:
            body = resp.read() if (method != "HEAD" and read_body) else b""
            status, hdrs = resp.status, dict(resp.headers.items())
            if cacheable and status == 200:
                _cache_store(url, status, hdrs, body)
            return status, hdrs, body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()) if e.headers else {}, (e.read() if read_body and e.headers else b"")
    except TimeoutError:
        return None, {}, f"TIMEOUT: no complete response within {TIMEOUT}s (file may just be large/slow, not necessarily down)".encode()
    except Exception as e:
        return None, {}, f"{type(e).__name__}: {e}".encode()


def check_http_metadata(url):
    """Runs the Appendix D checks: GET headers, HEAD support, conditional GET (304)."""
    status, headers, body = http_request(url, "GET")
    ctype = headers.get("Content-Type", "")
    etag = headers.get("ETag")
    last_mod = headers.get("Last-Modified")
    clen = headers.get("Content-Length")

    head_status, head_headers, _ = http_request(url, "HEAD")
    head_ok = head_status == 200

    cond_ok = None
    if etag:
        # Only the status matters here; do NOT re-download the body.
        cstatus, _, _ = http_request(url, "GET", headers={"If-None-Match": etag}, read_body=False)
        cond_ok = (cstatus == 304)

    stale = None
    if last_mod:
        try:
            lm_dt = parsedate_to_utc(last_mod)
            stale = (datetime.now(timezone.utc) - lm_dt).days > FRESHNESS_DAYS
        except Exception:
            stale = None

    meta = {
        "status": status,
        "content_type": ctype,
        "content_type_ok": "json" in ctype.lower(),
        "etag_present": bool(etag),
        "last_modified_present": bool(last_mod),
        "content_length_present": bool(clen),
        "head_supported": head_ok,
        "conditional_get_304": cond_ok,
        "stale_over_30_days": stale,
    }
    return meta, body


_CHALLENGE_MARKERS = (
    "captcha", "recaptcha", "hcaptcha", "cf-challenge", "cf_chl", "just a moment",
    "attention required", "checking your browser", "are you human", "access denied",
    "please enable javascript", "please enable cookies", "ddos protection", "bot detection",
    "verify you are human", "unusual traffic",
)


def detect_challenge_page(body, content_type):
    """Endpoints fronted by a bot-protection layer (Cloudflare, Akamai, a WAF)
    can return HTTP 200 with an HTML challenge/CAPTCHA page INSTEAD of the
    expected JSON. Left unchecked, that HTML gets fed straight into
    json.loads() and reported as a generic 'invalid JSON' (C4004/N3015) --
    technically true, but it hides the real cause and would have this
    validator silently try to run against a challenge page rather than the
    real data. This runs BEFORE any json.loads() call so the run stops with
    an accurate diagnosis instead.

    Returns a reason string if the body looks like a challenge/CAPTCHA page,
    else None."""
    ctype = (content_type or "").lower()
    if "json" in ctype:
        return None  # server explicitly says JSON; trust it over a keyword sniff
    text = body[:4096].decode("utf-8", "replace").lower() if isinstance(body, bytes) else str(body)[:4096].lower()
    if "html" in ctype or "<html" in text or "<!doctype html" in text:
        for marker in _CHALLENGE_MARKERS:
            if marker in text:
                return f"Response looks like a bot-protection/CAPTCHA challenge page (matched {marker!r}), not the expected JSON"
    return None


def _cert_time_to_utc(cert_time_str):
    """Parses the OpenSSL notBefore/notAfter format ('Jul  7 05:30:00 2026 GMT')
    into an aware UTC datetime."""
    return datetime.strptime(cert_time_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)


def _fmt_cert_dt(dt):
    """'Tuesday, 7 July 2026 at 05:30:00' -- matches how a browser's
    certificate-details panel displays Issued On / Expires On."""
    return f"{dt.strftime('%A')}, {dt.day} {dt.strftime('%B %Y')} at {dt.strftime('%H:%M:%S')}"


def check_tls_certificate(url):
    """C4002 (X509CertError), done at the socket/TLS layer instead of by
    opening the URL in a browser and clicking the padlock -- performs the
    same handshake + validation Chrome does (hostname match, chain trust,
    expiry), and returns what the browser's certificate panel would show.

    Returns a dict: ok (True/False/None), error, not_before, not_after,
    issuer, subject, expired, days_until_expiry. ok=None means the URL
    isn't https (nothing to check); ok=False means the handshake or chain
    validation itself failed -- the actual C4002 condition."""
    parsed = urlparse(url)
    host, port = parsed.hostname, parsed.port or 443
    if parsed.scheme != "https" or not host:
        return {"ok": None, "error": "URL is not https:// -- no TLS certificate to check",
                "not_before": None, "not_after": None, "issuer": None, "subject": None,
                "expired": None, "days_until_expiry": None}
    try:
        with socket.create_connection((host, port), timeout=NPI_LOOKUP_TIMEOUT) as sock:
            with CTX.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        not_before = _cert_time_to_utc(cert["notBefore"])
        not_after = _cert_time_to_utc(cert["notAfter"])
        now = datetime.now(timezone.utc)
        subject = ", ".join(f"{k}={v}" for rdn in cert.get("subject", ()) for k, v in rdn)
        issuer = ", ".join(f"{k}={v}" for rdn in cert.get("issuer", ()) for k, v in rdn)
        return {"ok": True, "error": None, "not_before": not_before, "not_after": not_after,
                "issuer": issuer or None, "subject": subject or None,
                "expired": now > not_after, "days_until_expiry": (not_after - now).days}
    except ssl.SSLCertVerificationError as e:
        return {"ok": False, "error": f"Certificate chain/hostname validation failed: {e}",
                "not_before": None, "not_after": None, "issuer": None, "subject": None,
                "expired": None, "days_until_expiry": None}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "not_before": None, "not_after": None, "issuer": None, "subject": None,
                "expired": None, "days_until_expiry": None}


def parsedate_to_utc(http_date_str):
    from email.utils import parsedate_to_datetime
    dt = parsedate_to_datetime(http_date_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso_to_utc(s):
    """Parses ISO 8601 date or datetime strings (with or without 'Z') to aware UTC datetime."""
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def extract_year_from_url(url):
    m = re.search(r"/(20\d{2})(?:/|$)", url)
    return m.group(1) if m else None


def is_placeholder_resource(resource):
    """HAPI FHIR marks an auto-created stub (a resource referenced elsewhere but
    never actually included in the export) with this extension. Its presence
    means the data has a dangling/broken reference."""
    for ext in resource.get("extension", []) or []:
        if ext.get("url") == PLACEHOLDER_EXT_URL and ext.get("valueBoolean") is True:
            return True
    return False


# ---------------------------------------------------------------------------
# Field-level validators (per-resource-type, works on entries merged across
# all constituent files of that type, e.g. practitionerrole-part1 + part2)
# ---------------------------------------------------------------------------
def get_identifier(resource, system):
    for ident in resource.get("identifier", []) or []:
        if ident.get("system") == system:
            return ident.get("value")
    return None


def freshness_count(entries, date_getter):
    now = datetime.now(timezone.utc)
    stale = missing = 0
    for e in entries:
        raw = date_getter(e)
        if not raw:
            missing += 1
            continue
        dt = parse_iso_to_utc(raw)
        if dt is None:
            missing += 1
            continue
        if (now - dt).days > FRESHNESS_DAYS:
            stale += 1
    return stale, missing


def period_date_fmt_bucket(resource_type, entries):
    b = Bucket()
    for e in entries:
        r = e.get("resource", {})
        period = r.get("period") or {}
        tag = tag_for(resource_type, r)
        for field in ("start", "end"):
            val = period.get(field)
            if val and not DATE_RE.match(str(val)[:10]):
                b.hit(f"{tag} period.{field}={val!r}")
    return b


def invalid_date_fmt_bucket(resource_type, entries, date_getter):
    b = Bucket()
    for e in entries:
        raw = date_getter(e)
        if raw and not DATE_RE.match(str(raw)[:10]):
            r = e.get("resource", e) if isinstance(e, dict) and "resource" in e else e
            b.hit(f"{tag_for(resource_type, r)} value={raw!r}")
    return b


def future_date_bucket(resource_type, entries, date_getter):
    now = datetime.now(timezone.utc)
    b = Bucket()
    for e in entries:
        raw = date_getter(e)
        if not raw:
            continue
        d = parse_iso_to_utc(raw)
        if d and d > now:
            r = e.get("resource", e) if isinstance(e, dict) and "resource" in e else e
            b.hit(f"{tag_for(resource_type, r)} value={raw!r}")
    return b


class Bucket:
    def __init__(self, limit=30):
        self.count = 0
        self.limit = limit
        self.examples = []
        self.pass_example = None
        self.pass_count = 0

    def hit(self, tag):
        self.count += 1
        if len(self.examples) < self.limit:
            self.examples.append(tag)

    def pass_hit(self, tag):
        self.pass_count += 1
        if self.pass_example is None:
            self.pass_example = tag

    def suffix(self):
        parts = []
        if self.examples:
            more = f", +{self.count - len(self.examples)} more" if self.count > len(self.examples) else ""
            parts.append(f"fail e.g. {'; '.join(self.examples)}{more}")
        if self.pass_example:
            parts.append(f"pass e.g. {self.pass_example}")
        return f" ({'; '.join(parts)})" if parts else ""


def tag_for(rtype, r):
    ident = resource_identifier(rtype, r) or f"id:{r.get('id')}"
    smile = meta_source(r)
    return f"{ident} [smile:{smile}]" if smile else f"{ident} [id:{r.get('id')}]"


def _phone_value(r):
    for t in r.get("telecom", []) or []:
        if t.get("system") == "phone" and t.get("value"):
            return t.get("value")
    return None


def _location_excluded_by_physical_type(r):
    codings = (r.get("physicalType") or {}).get("coding", []) or []
    if not codings:
        return False
    return not any(c.get("code") == "si" for c in codings)


def _ref_id(ref):
    return str(ref or "").split("/")[-1]


def _reference_has_identifier(reference_obj, system):
    ident = (reference_obj or {}).get("identifier") or {}
    return bool(ident.get("system") == system and ident.get("value"))


def build_fhir_context(bundles):
    ctx = {
        "role_phone_prac_ids": set(),
        "role_npi_prac_ids": set(),
        "loc_by_id": {},
        "loc_phone_ids": set(),
        "org_loc_ids": defaultdict(set),
        "prac_qualification_ids": set(),
        "prac_loc_ids": defaultdict(set),
        "prac_own_phone_ids": set(),
        "role_spec_prac_ids": set(),
    }
    for e in bundles.get("Practitioner", []):
        r = e.get("resource", {})
        pid = str(r.get("id"))
        if _phone_value(r):
            ctx["prac_own_phone_ids"].add(pid)
        if any((q.get("code") or {}).get("coding") for q in r.get("qualification", []) or []):
            ctx["prac_qualification_ids"].add(pid)
    for e in bundles.get("Location", []):
        r = e.get("resource", {})
        lid = str(r.get("id"))
        ctx["loc_by_id"][lid] = r
        if _phone_value(r):
            ctx["loc_phone_ids"].add(lid)
    for e in bundles.get("PractitionerRole", []):
        r = e.get("resource", {})
        pid = _ref_id((r.get("practitioner") or {}).get("reference"))
        if not pid:
            continue
        if _phone_value(r):
            ctx["role_phone_prac_ids"].add(pid)
        if get_identifier(r, NPI_SYS):
            ctx["role_npi_prac_ids"].add(pid)
        if any(c.get("code") for s in r.get("specialty", []) or [] for c in s.get("coding", []) or []):
            ctx["role_spec_prac_ids"].add(pid)
        for loc in r.get("location", []) or []:
            lid = _ref_id(loc.get("reference"))
            if lid:
                ctx["prac_loc_ids"][pid].add(lid)
    for e in bundles.get("OrganizationAffiliation", []):
        r = e.get("resource", {})
        oid = _ref_id((r.get("organization") or {}).get("reference"))
        if not oid:
            continue
        for loc in r.get("location", []) or []:
            lid = _ref_id(loc.get("reference"))
            if lid:
                ctx["org_loc_ids"][oid].add(lid)
    return ctx


EMPTY_CTX = {"role_phone_prac_ids": set(), "role_npi_prac_ids": set(),
             "loc_by_id": {}, "loc_phone_ids": set(), "org_loc_ids": {},
             "prac_qualification_ids": set(), "prac_loc_ids": {},
             "prac_own_phone_ids": set(), "role_spec_prac_ids": set()}


def _org_fallback_locations(ctx, org_id):
    return [ctx["loc_by_id"][lid] for lid in ctx["org_loc_ids"].get(org_id, set())
            if lid in ctx["loc_by_id"]]


def print_coverage_block(resource_type, check_label, coverage, not_tested_limit=10):
    total = coverage["total"]
    tested = coverage["tested"]
    not_tested_records = coverage["not_tested_records"]
    not_tested = total - tested
    pct = (100.0 * tested / total) if total else 0.0
    pct_str = "100.00%" if not_tested == 0 else f"{pct:.2f}%"

    if not not_tested_records:
        print(f"  [coverage] {resource_type:<24} {check_label:<45} {tested:>6,}/{total:<6,} tested ({pct_str})")
        return

    print(f"  --------------------------------------------------------------")
    print(f"  Resource Type: {resource_type}")
    print(f"  Test Case: {check_label}")
    print(f"  Total Count: {total:,}")
    print(f"  Tested Count: {tested:,}")
    print(f"  Not Tested Count: {not_tested:,}")
    print(f"  Test Coverage: {pct_str}")
    preview = not_tested_records[:not_tested_limit]
    for rid, ident in preview:
        print(f"    Not Tested -> Resource ID: {rid}, Identifier: {ident}")
    if len(not_tested_records) > not_tested_limit:
        print(f"    ... +{len(not_tested_records) - not_tested_limit} more not-tested record(s)")


def validate_fhir_bundle(resource_type, entries, ctx=None, org=None):
    """Returns list of (check_name, result, detail) tuples for one FHIR resource type."""
    ctx = ctx or EMPTY_CTX
    checks = []
    n = len(entries)
    if n == 0:
        return checks, {"total": 0, "tested": 0, "not_tested_records": []}

    def multi_npi_bucket(entries):
        b = Bucket()
        for e in entries:
            r = e.get("resource", {})
            npis = [i.get("value") for i in r.get("identifier", []) or [] if i.get("system") == NPI_SYS]
            if len(npis) > 1:
                b.hit(tag_for(resource_type, r))
        return b

    if resource_type == "Practitioner":
        missing_npi = Bucket(); missing_first = Bucket(); missing_last = Bucket()
        missing_phone = Bucket(); invalid_phone = Bucket(); missing_sex = Bucket()
        missing_lang = Bucket(); bad_npi_fmt = Bucket(); missing_fulltext = Bucket()
        missing_addr = Bucket()
        missing_city = Bucket(); missing_state = Bucket(); invalid_state = Bucket()
        missing_zip = Bucket(); invalid_zip = Bucket(); missing_street = Bucket()
        missing_spec = Bucket()
        for e in entries:
            r = e.get("resource", {})
            rid = str(r.get("id"))
            tag = tag_for(resource_type, r)
            if any((q.get("code") or {}).get("coding") for q in r.get("qualification", []) or []):
                missing_spec.pass_hit(f"{tag} (taxonomy on Practitioner.qualification)")
            elif rid in ctx["role_spec_prac_ids"]:
                missing_spec.pass_hit(f"{tag} (specialty on PractitionerRole)")
            else:
                missing_spec.hit(tag)
            linked_loc_ids = ctx["prac_loc_ids"].get(rid, set())
            linked_locs = [ctx["loc_by_id"][lid] for lid in linked_loc_ids if lid in ctx["loc_by_id"]]
            locs_with_addr = [l for l in linked_locs if (l.get("address") or {})]
            if not locs_with_addr:
                missing_addr.hit(f"{tag} ({len(linked_locs)} linked Location(s), none with an address)")
            else:
                missing_addr.pass_hit(f"{tag} (address via linked Location)")
                addr = locs_with_addr[0].get("address") or {}
                if not addr.get("line"):
                    missing_street.hit(tag)
                if not addr.get("city"):
                    missing_city.hit(tag)
                state = addr.get("state")
                if not state:
                    missing_state.hit(tag)
                elif not state_valid(state):
                    invalid_state.hit(f"{tag} state={state!r}")
                zip_code = addr.get("postalCode")
                if not zip_code:
                    missing_zip.hit(tag)
                elif not zip_base_valid(zip_code):
                    invalid_zip.hit(f"{tag} postalCode={zip_code!r}")
            npi = get_identifier(r, NPI_SYS)
            if not npi:
                if rid in ctx["role_npi_prac_ids"]:
                    missing_npi.pass_hit(f"{tag} (NPI on PractitionerRole)")
                else:
                    missing_npi.hit(tag)
            elif not NPI_RE.match(str(npi)):
                bad_npi_fmt.hit(tag)
            name = (r.get("name") or [{}])[0]
            if not name.get("given"):
                missing_first.hit(tag)
            if not name.get("family"):
                missing_last.hit(tag)
            if not name.get("text"):
                missing_fulltext.hit(tag)
            phone_entry = next((t for t in r.get("telecom", []) or [] if t.get("system") == "phone"), None)
            if not phone_entry:
                if rid in ctx["role_phone_prac_ids"]:
                    missing_phone.pass_hit(f"{tag} (phone on PractitionerRole)")
                else:
                    missing_phone.hit(tag)
            else:
                phone_val = str(phone_entry.get("value") or "").strip()
                if not PHONE_RE.match(phone_val):
                    invalid_phone.hit(f"{tag} phone={phone_entry.get('value')!r}")
            if not r.get("gender"):
                missing_sex.hit(tag)
            if not r.get("communication"):
                missing_lang.hit(tag)
        checks.append((code_row_suffix("P1001"), "PASS" if not missing_npi.count else "FAIL",
                        f"{missing_npi.count} of {n} Practitioner entries have no NPI identifier{missing_npi.suffix()}"))
        checks.append((code_row_suffix("P1006"), "PASS" if not missing_first.count else "FAIL",
                        f"{missing_first.count} of {n} entries missing given (first) name{missing_first.suffix()}"))
        checks.append((code_row_suffix("P1007"), "PASS" if not missing_last.count else "FAIL",
                        f"{missing_last.count} of {n} entries missing family (last) name{missing_last.suffix()}"))
        checks.append((code_row_suffix("A2009"), "PASS" if not missing_phone.count else "FAIL",
                        f"{missing_phone.count} of {n} entries have no phone telecom entry on either the "
                        f"Practitioner or a linked PractitionerRole (Appendix B 7a/7b){missing_phone.suffix()}"))
        checks.append((code_row_suffix("P1009"), "PASS" if not missing_spec.count else "FAIL",
                        f"{missing_spec.count} of {n} entries have no taxonomy on Practitioner.qualification "
                        f"AND no specialty on the linked PractitionerRole{missing_spec.suffix()}"))
        checks.append((code_row_suffix("A2001"), "PASS" if not missing_addr.count else "FAIL",
                        f"{missing_addr.count} of {n} entries have no address resolvable via any linked "
                        f"PractitionerRole -> Location{missing_addr.suffix()}"))
        checks.append((code_row_suffix("A2007"), "PASS" if not missing_street.count else "FAIL",
                        f"{missing_street.count} of {n} entries have a resolved Location address missing street line{missing_street.suffix()}"))
        checks.append((code_row_suffix("A2002"), "PASS" if not missing_city.count else "FAIL",
                        f"{missing_city.count} of {n} entries have a resolved Location address missing city{missing_city.suffix()}"))
        checks.append((code_row_suffix("A2003"), "PASS" if not missing_state.count else "FAIL",
                        f"{missing_state.count} of {n} entries have a resolved Location address missing state{missing_state.suffix()}"))
        checks.append((code_row_suffix("A2004"), "PASS" if not invalid_state.count else "FAIL",
                        f"{invalid_state.count} of {n} entries have a resolved Location address state not matching 2-letter format{invalid_state.suffix()}"))
        checks.append((code_row_suffix("A2005"), "PASS" if not missing_zip.count else "FAIL",
                        f"{missing_zip.count} of {n} entries have a resolved Location address missing postal code{missing_zip.suffix()}"))
        checks.append((code_row_suffix("A2006"), "PASS" if not invalid_zip.count else "FAIL",
                        f"{invalid_zip.count} of {n} entries have a resolved Location address postal code not matching 5-digit or ZIP+4 format{invalid_zip.suffix()}"))
        checks.append(("Practitioner full name (name.text, Appendix B item 5->i, required)",
                        "PASS" if not missing_fulltext.count else "FAIL",
                        f"{missing_fulltext.count} of {n} entries have no name.text (full name){missing_fulltext.suffix()}"))
        checks.append((code_row_suffix("A2010"), "PASS" if not invalid_phone.count else "FAIL",
                        f"{invalid_phone.count} of {n} entries have a phone number not matching 10-digit format{invalid_phone.suffix()}"))
        checks.append((code_row_suffix("P1010"), "PASS" if not missing_sex.count else "FAIL",
                        f"{missing_sex.count} of {n} entries have no gender element{missing_sex.suffix()}"))
        checks.append((code_row_suffix("P1011"), "PASS" if not missing_lang.count else "FAIL",
                        f"{missing_lang.count} of {n} entries have no communication/language element{missing_lang.suffix()}"))
        checks.append(("NPI format (10 digits)", "PASS" if not bad_npi_fmt.count else "FAIL",
                        f"{bad_npi_fmt.count} of {n} NPIs are not exactly 10 digits{bad_npi_fmt.suffix()}"))
        multi = multi_npi_bucket(entries)
        checks.append((code_row_suffix("F5009"), "PASS" if not multi.count else "FAIL",
                        f"{multi.count} of {n} Practitioner entries carry more than one us-npi identifier{multi.suffix()}"))
        stale, missing_dt = freshness_count(entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
        checks.append((code_row_suffix("C4010"), "FAIL" if stale else "PASS",
                        f"{stale} of {n} records last updated more than {FRESHNESS_DAYS} days ago; {missing_dt} have no lastUpdated"))
        bad_dt_fmt = invalid_date_fmt_bucket(resource_type, entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
        checks.append((code_row_suffix("P1013"), "PASS" if not bad_dt_fmt.count else "FAIL",
                        f"{bad_dt_fmt.count} of {n} entries have a meta.lastUpdated date not in YYYY-MM-DD format (e.g. DD-MM-YYYY is rejected){bad_dt_fmt.suffix()}"))
        future_dt = future_date_bucket(resource_type, entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
        checks.append((code_row_suffix("P1014"), "PASS" if not future_dt.count else "FAIL",
                        f"{future_dt.count} of {n} entries have a meta.lastUpdated timestamp in the future{future_dt.suffix()}"))

    elif resource_type == "PractitionerRole":
        missing_loc = Bucket(); missing_spec = Bucket(); missing_net = Bucket(); missing_prac = Bucket()
        missing_accepting = Bucket(); invalid_accepting = Bucket(); future_dt = Bucket()
        invalid_phone = Bucket(); missing_phone = Bucket(); not_associated = Bucket()
        network_id_sys = NETWORK_IDENTIFIER_SYSTEM_BY_ORG.get(org)
        VALID_NEWPT_CODES = {"nopt", "newpt", "existptonly", "existptfam"}
        for e in entries:
            r = e.get("resource", {})
            tag = tag_for(resource_type, r)
            if not r.get("location"):
                missing_loc.hit(tag)
            if network_id_sys:
                network_ext = next((ext for ext in r.get("extension", []) or []
                                    if "network" in (ext.get("url") or "").lower()), None)
                network_ref = (network_ext or {}).get("valueReference")
                if _reference_has_identifier(network_ref, network_id_sys):
                    not_associated.pass_hit(tag)
                else:
                    not_associated.hit(tag)
            if not any(c.get("code") for s in r.get("specialty", []) or [] for c in s.get("coding", []) or []):
                prac_id = _ref_id((r.get("practitioner") or {}).get("reference"))
                if prac_id in ctx["prac_qualification_ids"]:
                    missing_spec.pass_hit(f"{tag} (taxonomy on Practitioner.qualification)")
                else:
                    missing_spec.hit(tag)
            if not any("network" in (ext.get("url") or "").lower() for ext in r.get("extension", []) or []):
                missing_net.hit(tag)
            if not r.get("practitioner"):
                missing_prac.hit(tag)

            newpt_ext = next((ext for ext in r.get("extension", []) or []
                              if "newpatients" in (ext.get("url") or "").lower()), None)
            if not newpt_ext:
                missing_accepting.hit(tag)
            else:
                codes = [c.get("code") for sub in newpt_ext.get("extension", []) or []
                         for c in [sub.get("valueCodeableConcept", {}) or {}]
                         for c in c.get("coding", []) or []]
                if not codes or not any(c in VALID_NEWPT_CODES for c in codes):
                    invalid_accepting.hit(f"{tag} codes={codes}")
                else:
                    invalid_accepting.pass_hit(f"{tag} codes={codes}")

            last_updated = r.get("meta", {}).get("lastUpdated")
            if last_updated:
                d = parse_iso_to_utc(last_updated)
                if d and d > datetime.now(timezone.utc):
                    future_dt.hit(f"{tag} lastUpdated={last_updated}")

            phone_entry = next((t for t in r.get("telecom", []) or [] if t.get("system") == "phone"), None)
            if phone_entry:
                phone_val = str(phone_entry.get("value") or "").strip()
                if not PHONE_RE.match(phone_val):
                    invalid_phone.hit(f"{tag} phone={phone_entry.get('value')!r}")
            else:
                prac_id = _ref_id((r.get("practitioner") or {}).get("reference"))
                if prac_id in ctx["prac_own_phone_ids"]:
                    missing_phone.pass_hit(f"{tag} (phone on Practitioner)")
                else:
                    missing_phone.hit(tag)

        checks.append((code_row_suffix("F5003"), "PASS" if not missing_prac.count else "FAIL",
                        f"{missing_prac.count} of {n} entries have no practitioner reference{missing_prac.suffix()}"))
        checks.append((code_row_suffix("F5004"), "PASS" if not missing_loc.count else "FAIL",
                        f"{missing_loc.count} of {n} entries have no location reference{missing_loc.suffix()}"))
        checks.append((code_row_suffix("P1009"), "PASS" if not missing_spec.count else "FAIL",
                        f"{missing_spec.count} of {n} entries have no specialty code on PractitionerRole.specialty "
                        f"AND no taxonomy on the linked Practitioner.qualification{missing_spec.suffix()}"))
        checks.append((code_row_suffix("F5001"), "PASS" if not missing_net.count else "FAIL",
                        f"{missing_net.count} of {n} entries have no network-reference extension{missing_net.suffix()}"))
        checks.append((code_row_suffix("P1012"), "PASS" if not missing_accepting.count else "FAIL",
                        f"{missing_accepting.count} of {n} entries have no accepting-new-patients extension{missing_accepting.suffix()}"))
        checks.append((code_row_suffix("P1018"), "PASS" if not invalid_accepting.count else "FAIL",
                        f"{invalid_accepting.count} of {n} entries have an accepting-new-patients code not in "
                        f"{{nopt, newpt, existptonly, existptfam}}{invalid_accepting.suffix()}"))
        multi = multi_npi_bucket(entries)
        checks.append((code_row_suffix("F5009"), "PASS" if not multi.count else "FAIL",
                        f"{multi.count} of {n} PractitionerRole entries carry more than one us-npi identifier{multi.suffix()}"))
        stale, missing_dt = freshness_count(entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
        checks.append((code_row_suffix("C4010"), "FAIL" if stale else "PASS",
                        f"{stale} of {n} records last updated more than {FRESHNESS_DAYS} days ago; {missing_dt} have no lastUpdated"))
        checks.append((code_row_suffix("P1014"), "PASS" if not future_dt.count else "FAIL",
                        f"{future_dt.count} of {n} entries have a meta.lastUpdated timestamp in the future{future_dt.suffix()}"))
        bad_dt_fmt = invalid_date_fmt_bucket(resource_type, entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
        checks.append((code_row_suffix("P1013"), "PASS" if not bad_dt_fmt.count else "FAIL",
                        f"{bad_dt_fmt.count} of {n} entries have a meta.lastUpdated date not in YYYY-MM-DD format (e.g. DD-MM-YYYY is rejected){bad_dt_fmt.suffix()}"))
        checks.append((code_row_suffix("A2010") + " (PractitionerRole.telecom)", "PASS" if not invalid_phone.count else "FAIL",
                        f"{invalid_phone.count} of {n} entries have a PractitionerRole phone not matching 10-digit format{invalid_phone.suffix()}"))
        checks.append((code_row_suffix("A2009") + " (PractitionerRole.telecom)", "PASS" if not missing_phone.count else "FAIL",
                        f"{missing_phone.count} of {n} entries have no phone on PractitionerRole.telecom "
                        f"AND none on the linked Practitioner.telecom (Appendix B 7a/7b){missing_phone.suffix()}"))
        if network_id_sys:
            checks.append((code_row_suffix("P1016"), "PASS" if not not_associated.count else "FAIL",
                            f"{not_associated.count} of {n} entries have no identifier under {network_id_sys} "
                            f"(not associated with a real network){not_associated.suffix()}"))
        else:
            checks.append((code_row_suffix("P1016"), "INFO",
                            f"No known network-identifier system configured for org={org!r}; check skipped"))

    elif resource_type == "Location":
        missing_line = Bucket(); missing_city = Bucket(); missing_state = Bucket(); missing_zip = Bucket()
        invalid_state = Bucket(); invalid_zip = Bucket(); invalid_phone = Bucket()
        missing_addr = Bucket(); missing_phone = Bucket()
        tested_n = 0
        for e in entries:
            r = e.get("resource", {})
            addr = r.get("address", {}) or {}
            tag = tag_for(resource_type, r)
            if not addr:
                missing_addr.hit(tag)
            state = addr.get("state")
            if not state:
                missing_state.hit(tag)
            elif not state_valid(state):
                invalid_state.hit(f"{tag} state={state!r}")
            else:
                invalid_state.pass_hit(f"{tag} state={state!r}")
            if _location_excluded_by_physical_type(r):
                continue
            tested_n += 1
            phone = _phone_value(r)
            if phone:
                phone_digits = str(phone).strip()
                if not PHONE_RE.match(phone_digits):
                    invalid_phone.hit(f"{tag} phone={phone!r}")
            else:
                missing_phone.hit(tag)
            if not addr.get("line"):
                missing_line.hit(tag)
            if not addr.get("city"):
                missing_city.hit(tag)
            zip_code = addr.get("postalCode")
            if not zip_code:
                missing_zip.hit(tag)
            elif not zip_base_valid(zip_code):
                invalid_zip.hit(f"{tag} postalCode={zip_code!r}")
            else:
                invalid_zip.pass_hit(f"{tag} postalCode={zip_code!r}")
        checks.append((code_row_suffix("A2001"), "PASS" if not missing_addr.count else "FAIL",
                        f"{missing_addr.count} of {n} Location entries have no address element at all{missing_addr.suffix()}"))
        checks.append((code_row_suffix("A2007"), "PASS" if not missing_line.count else "FAIL",
                        f"{missing_line.count} of {tested_n} Location entries missing address line{missing_line.suffix()}"))
        checks.append((code_row_suffix("A2002"), "PASS" if not missing_city.count else "FAIL",
                        f"{missing_city.count} of {tested_n} Location entries missing city{missing_city.suffix()}"))
        checks.append((code_row_suffix("A2003"), "PASS" if not missing_state.count else "FAIL",
                        f"{missing_state.count} of {n} Location entries missing state{missing_state.suffix()}"))
        checks.append((code_row_suffix("A2004"), "PASS" if not invalid_state.count else "FAIL",
                        f"{invalid_state.count} of {n} Location entries have a state not matching 2-letter format{invalid_state.suffix()}"))
        checks.append((code_row_suffix("A2005"), "PASS" if not missing_zip.count else "FAIL",
                        f"{missing_zip.count} of {tested_n} Location entries missing postal code{missing_zip.suffix()}"))
        checks.append((code_row_suffix("A2006"), "PASS" if not invalid_zip.count else "FAIL",
                        f"{invalid_zip.count} of {tested_n} Location entries have a postal code not matching 5-digit or ZIP+4 format{invalid_zip.suffix()}"))
        bad_period_fmt = period_date_fmt_bucket(resource_type, entries)
        checks.append((code_row_suffix("P1013"), "PASS" if not bad_period_fmt.count else "FAIL",
                        f"{bad_period_fmt.count} of {n} Location entries have a period.start/end date not in YYYY-MM-DD format (e.g. DD-MM-YYYY is rejected){bad_period_fmt.suffix()}"))
        checks.append((code_row_suffix("A2010") + " (Location.telecom)", "PASS" if not invalid_phone.count else "FAIL",
                        f"{invalid_phone.count} of {tested_n} Location entries have a phone not matching 10-digit format{invalid_phone.suffix()}"))
        checks.append((code_row_suffix("A2009") + " (Location.telecom)", "PASS" if not missing_phone.count else "FAIL",
                        f"{missing_phone.count} of {tested_n} Location entries have no phone on Location.telecom{missing_phone.suffix()}"))
        loc_stale, loc_missing_dt = freshness_count(entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
        checks.append((code_row_suffix("C4010"), "FAIL" if loc_stale else "PASS",
                        f"{loc_stale} of {n} Location records last updated more than {FRESHNESS_DAYS} days ago; {loc_missing_dt} have no lastUpdated"))
        loc_future_dt = future_date_bucket(resource_type, entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
        checks.append((code_row_suffix("P1014"), "PASS" if not loc_future_dt.count else "FAIL",
                        f"{loc_future_dt.count} of {n} Location entries have a meta.lastUpdated timestamp in the future{loc_future_dt.suffix()}"))

    elif resource_type == "Organization":
        fac = ntwk = 0
        missing_npi = Bucket(); missing_name = Bucket(); missing_addr = Bucket(); bad_npi_fmt = Bucket()
        wrong_type_sys = Bucket(); missing_fac_code = Bucket(); missing_phone = Bucket(); invalid_phone = Bucket()
        missing_line = Bucket(); missing_city = Bucket(); missing_state = Bucket(); invalid_state = Bucket()
        missing_zip = Bucket(); invalid_zip = Bucket()
        fac_entries = []
        ntwk_records = []
        for e in entries:
            r = e.get("resource", {})
            rid = str(r.get("id"))
            codings = [c for t in r.get("type", []) or [] for c in t.get("coding", []) or []]
            codes = [c.get("code") for c in codings]
            all_org_phone = _phone_value(r)
            if all_org_phone:
                all_org_phone_digits = str(all_org_phone).strip()
                if not PHONE_RE.match(all_org_phone_digits):
                    invalid_phone.hit(f"{tag_for(resource_type, r)} phone={all_org_phone!r}")
            if "ntwk" in codes:
                ntwk += 1
                ntwk_records.append((rid, resource_identifier(resource_type, r) or f"id:{rid}"))
                continue
            fac += 1
            fac_entries.append(e)
            tag = tag_for(resource_type, r)
            if "fac" not in codes:
                missing_fac_code.hit(f"{tag} type codes present: {', '.join(c for c in codes if c) or 'none'}")
            elif not any(c.get("code") == "fac" and c.get("system") == ORGTYPE_SYS for c in codings):
                fac_systems = sorted({c.get("system") or "(no system)" for c in codings if c.get("code") == "fac"})
                wrong_type_sys.hit(f"{tag} system(s)={fac_systems}")
            else:
                wrong_type_sys.pass_hit(tag)
            npi = get_identifier(r, NPI_SYS)
            if not npi:
                missing_npi.hit(tag)
            elif not NPI_RE.match(str(npi)):
                bad_npi_fmt.hit(tag)
            if not r.get("name"):
                missing_name.hit(tag)
            fallback_locs = _org_fallback_locations(ctx, rid)
            org_addrs = r.get("address") or []
            if isinstance(org_addrs, dict):
                org_addrs = [org_addrs]
            if org_addrs:
                for addr in org_addrs:
                    if not addr.get("line"):
                        missing_line.hit(tag)
                    if not addr.get("city"):
                        missing_city.hit(tag)
                    state = addr.get("state")
                    if not state:
                        missing_state.hit(tag)
                    elif not state_valid(state):
                        invalid_state.hit(f"{tag} state={state!r}")
                    zip_code = addr.get("postalCode")
                    if not zip_code:
                        missing_zip.hit(tag)
                    elif not zip_base_valid(zip_code):
                        invalid_zip.hit(f"{tag} postalCode={zip_code!r}")
            elif any((l.get("address") or {}) for l in fallback_locs):
                missing_addr.pass_hit(f"{tag} (address via linked Location)")
            else:
                missing_addr.hit(tag)
            phone = _phone_value(r)
            if phone:
                pass
            elif any(str(l.get("id")) in ctx["loc_phone_ids"] for l in fallback_locs):
                missing_phone.pass_hit(f"{tag} (phone on linked Location)")
            else:
                missing_phone.hit(tag)
        checks.append((code_row_suffix("P1001"), "PASS" if not missing_npi.count else "FAIL",
                        f"{missing_npi.count} of {fac} facility Organization entries have no NPI identifier{missing_npi.suffix()}"))
        checks.append((code_row_suffix("P1008"), "PASS" if not missing_name.count else "FAIL",
                        f"{missing_name.count} of {fac} facility entries missing name{missing_name.suffix()}"))
        checks.append((code_row_suffix("A2001"), "PASS" if not missing_addr.count else "FAIL",
                        f"{missing_addr.count} of {fac} facility entries have no Organization.address AND no "
                        f"address resolvable via a linked Location (Appendix B 7a/7b){missing_addr.suffix()}"))
        checks.append((code_row_suffix("P1004") + " (fac code missing)", "PASS" if not missing_fac_code.count else "FAIL",
                        f"{missing_fac_code.count} of {fac} facility entries have no 'fac' type code at all "
                        f"(e.g. a different code like 'prvgrp' or 'bus' instead){missing_fac_code.suffix()}"))
        checks.append((code_row_suffix("P1004") + " (OrgTypeCS system)", "PASS" if not wrong_type_sys.count else "FAIL",
                        f"{wrong_type_sys.count} of {fac} facility entries have a 'fac' type code without "
                        f"system={ORGTYPE_SYS}{wrong_type_sys.suffix()}"))
        checks.append((code_row_suffix("A2007"), "PASS" if not missing_line.count else "FAIL",
                        f"{missing_line.count} of {fac} facility Organization.address entries missing address line{missing_line.suffix()}"))
        checks.append((code_row_suffix("A2002"), "PASS" if not missing_city.count else "FAIL",
                        f"{missing_city.count} of {fac} facility Organization.address entries missing city{missing_city.suffix()}"))
        checks.append((code_row_suffix("A2003"), "PASS" if not missing_state.count else "FAIL",
                        f"{missing_state.count} of {fac} facility Organization.address entries missing state{missing_state.suffix()}"))
        checks.append((code_row_suffix("A2004"), "PASS" if not invalid_state.count else "FAIL",
                        f"{invalid_state.count} of {fac} facility Organization.address entries have a state not matching 2-letter format{invalid_state.suffix()}"))
        checks.append((code_row_suffix("A2005"), "PASS" if not missing_zip.count else "FAIL",
                        f"{missing_zip.count} of {fac} facility Organization.address entries missing postal code{missing_zip.suffix()}"))
        checks.append((code_row_suffix("A2006"), "PASS" if not invalid_zip.count else "FAIL",
                        f"{invalid_zip.count} of {fac} facility Organization.address entries have a postal code not matching 5-digit or ZIP+4 format{invalid_zip.suffix()}"))
        checks.append((code_row_suffix("A2009") + " (facility)", "PASS" if not missing_phone.count else "FAIL",
                        f"{missing_phone.count} of {fac} facility entries have no phone on Organization.telecom "
                        f"or a linked Location.telecom (Appendix B 8a/8b){missing_phone.suffix()}"))
        checks.append((code_row_suffix("A2010"), "PASS" if not invalid_phone.count else "FAIL",
                        f"{invalid_phone.count} of {n} Organization entries (facility AND network) have a phone number not matching 10-digit format{invalid_phone.suffix()}"))
        checks.append(("NPI format (10 digits, facility)", "PASS" if not bad_npi_fmt.count else "FAIL",
                        f"{bad_npi_fmt.count} of {fac} facility NPIs are not exactly 10 digits{bad_npi_fmt.suffix()}"))
        multi = multi_npi_bucket(fac_entries)
        checks.append((code_row_suffix("F5009"), "PASS" if not multi.count else "FAIL",
                        f"{multi.count} of {fac} facility Organization entries carry more than one us-npi identifier{multi.suffix()}"))
        if fac_entries:
            stale, missing_dt = freshness_count(fac_entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
            checks.append((code_row_suffix("C4010"), "FAIL" if stale else "PASS",
                            f"{stale} of {fac} facility records last updated more than {FRESHNESS_DAYS} days ago; {missing_dt} have no lastUpdated"))
            bad_dt_fmt = invalid_date_fmt_bucket(resource_type, fac_entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
            checks.append((code_row_suffix("P1013") + " (meta.lastUpdated)", "PASS" if not bad_dt_fmt.count else "FAIL",
                            f"{bad_dt_fmt.count} of {fac} facility entries have a meta.lastUpdated date not in YYYY-MM-DD format (e.g. DD-MM-YYYY is rejected){bad_dt_fmt.suffix()}"))
            bad_period_fmt = period_date_fmt_bucket(resource_type, fac_entries)
            checks.append((code_row_suffix("P1013") + " (period)", "PASS" if not bad_period_fmt.count else "FAIL",
                            f"{bad_period_fmt.count} of {fac} facility entries have a period.start/end date not in YYYY-MM-DD format (e.g. DD-MM-YYYY is rejected){bad_period_fmt.suffix()}"))
            org_future_dt = future_date_bucket(resource_type, fac_entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
            checks.append((code_row_suffix("P1014"), "PASS" if not org_future_dt.count else "FAIL",
                            f"{org_future_dt.count} of {fac} facility entries have a meta.lastUpdated timestamp in the future{org_future_dt.suffix()}"))

    elif resource_type == "OrganizationAffiliation":
        missing_net = Bucket(); missing_org = Bucket(); missing_loc = Bucket(); missing_spec = Bucket()
        missing_phone = Bucket(); invalid_phone = Bucket(); not_associated = Bucket()
        network_id_sys = NETWORK_IDENTIFIER_SYSTEM_BY_ORG.get(org)
        for e in entries:
            r = e.get("resource", {})
            tag = tag_for(resource_type, r)
            if not r.get("network"):
                missing_net.hit(tag)
            if network_id_sys:
                if any(_reference_has_identifier(ref, network_id_sys) for ref in r.get("network", []) or []):
                    not_associated.pass_hit(tag)
                else:
                    not_associated.hit(tag)
            if not r.get("organization"):
                missing_org.hit(tag)
            if not r.get("location"):
                missing_loc.hit(tag)
            if not any(c.get("code") for s in r.get("specialty", []) or [] for c in s.get("coding", []) or []):
                missing_spec.hit(tag)
            phone = _phone_value(r)
            if phone:
                phone_digits = str(phone).strip()
                if not PHONE_RE.match(phone_digits):
                    invalid_phone.hit(f"{tag} phone={phone!r}")
            else:
                missing_phone.hit(tag)
        checks.append((code_row_suffix("F5001"), "PASS" if not missing_net.count else "FAIL",
                        f"{missing_net.count} of {n} entries have no network reference{missing_net.suffix()}"))
        checks.append((code_row_suffix("F5002"), "PASS" if not missing_org.count else "FAIL",
                        f"{missing_org.count} of {n} entries have no organization reference{missing_org.suffix()}"))
        checks.append((code_row_suffix("F5004"), "PASS" if not missing_loc.count else "FAIL",
                        f"{missing_loc.count} of {n} entries have no location reference{missing_loc.suffix()}"))
        checks.append((code_row_suffix("P1009"), "PASS" if not missing_spec.count else "FAIL",
                        f"{missing_spec.count} of {n} entries have no specialty code{missing_spec.suffix()}"))
        checks.append((code_row_suffix("A2009"), "PASS" if not missing_phone.count else "FAIL",
                        f"{missing_phone.count} of {n} entries have no phone on OrganizationAffiliation.telecom{missing_phone.suffix()}"))
        checks.append((code_row_suffix("A2010"), "PASS" if not invalid_phone.count else "FAIL",
                        f"{invalid_phone.count} of {n} entries have a phone not matching 10-digit format{invalid_phone.suffix()}"))
        if network_id_sys:
            checks.append((code_row_suffix("P1016"), "PASS" if not not_associated.count else "FAIL",
                            f"{not_associated.count} of {n} entries have no identifier under {network_id_sys} "
                            f"(not associated with a real network){not_associated.suffix()}"))
        else:
            checks.append((code_row_suffix("P1016"), "INFO",
                            f"No known network-identifier system configured for org={org!r}; check skipped"))
        oa_stale, oa_missing_dt = freshness_count(entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
        checks.append((code_row_suffix("C4010"), "FAIL" if oa_stale else "PASS",
                        f"{oa_stale} of {n} OrganizationAffiliation records last updated more than {FRESHNESS_DAYS} days ago; {oa_missing_dt} have no lastUpdated"))
        oa_bad_dt_fmt = invalid_date_fmt_bucket(resource_type, entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
        checks.append((code_row_suffix("P1013"), "PASS" if not oa_bad_dt_fmt.count else "FAIL",
                        f"{oa_bad_dt_fmt.count} of {n} entries have a meta.lastUpdated date not in YYYY-MM-DD format (e.g. DD-MM-YYYY is rejected){oa_bad_dt_fmt.suffix()}"))
        oa_future_dt = future_date_bucket(resource_type, entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
        checks.append((code_row_suffix("P1014"), "PASS" if not oa_future_dt.count else "FAIL",
                        f"{oa_future_dt.count} of {n} entries have a meta.lastUpdated timestamp in the future{oa_future_dt.suffix()}"))

    elif resource_type == "InsurancePlan":
        years = Counter()
        plan_ids = []
        missing_network = Bucket(); missing_pid = Bucket(); malformed_pid = Bucket(); missing_year = Bucket()
        omitted_contract = Bucket(); omitted_planid = Bucket(); omitted_segment = Bucket()
        bad_period_fmt = Bucket()
        unknown_contract = Bucket(); unknown_plan = Bucket(); unknown_segment = Bucket()
        registry = _known_id_registry(org)
        for e in entries:
            r = e.get("resource", {})
            tag = tag_for(resource_type, r)
            period = r.get("period", {}) or {}
            period_start = period.get("start") or ""
            yr = period_start[:4]
            years[yr] += 1
            if not yr:
                missing_year.hit(tag)
            elif not DATE_RE.match(period_start[:10]):
                bad_period_fmt.hit(f"{tag} period.start={period_start!r}")
            period_end = period.get("end")
            if period_end and not DATE_RE.match(str(period_end)[:10]):
                bad_period_fmt.hit(f"{tag} period.end={period_end!r}")
            pid = get_identifier(r, CMS_PLAN_SYS)
            if not pid:
                missing_pid.hit(tag)
            else:
                if not MAPLANID_RE.match(pid):
                    malformed_pid.hit(tag)
                else:
                    plan_ids.append(pid)
                contract_part, planid_part, segment_part = _parse_ma_plan_id(pid)
                if not contract_part:
                    omitted_contract.hit(f"{tag} raw={pid!r}")
                if not planid_part:
                    omitted_planid.hit(f"{tag} raw={pid!r}")
                if not segment_part:
                    omitted_segment.hit(f"{tag} raw={pid!r}")
                if registry and MAPLANID_RE.match(pid):
                    if contract_part not in registry["contracts"]:
                        unknown_contract.hit(f"{tag} raw={pid!r}")
                    elif (contract_part, planid_part) not in registry["plans"]:
                        unknown_plan.hit(f"{tag} raw={pid!r}")
                    elif pid not in registry["full"]:
                        unknown_segment.hit(f"{tag} raw={pid!r}")
                    else:
                        unknown_contract.pass_hit(tag)
                        unknown_plan.pass_hit(tag)
                        unknown_segment.pass_hit(tag)
            if not r.get("network"):
                missing_network.hit(tag)
        checks.append((code_row_suffix("N3001"), "PASS" if not missing_pid.count else "FAIL",
                        f"{missing_pid.count} of {n} InsurancePlan entries have no CMS ma-plan-id identifier{missing_pid.suffix()}"))
        checks.append((code_row_suffix("N3002"), "PASS" if not malformed_pid.count else "FAIL",
                        f"{malformed_pid.count} of {n} entries have an ma-plan-id not matching H####-###-### (e.g. blank segment){malformed_pid.suffix()}"))
        checks.append((code_row_suffix("N3006"), "PASS" if not omitted_contract.count else "FAIL",
                        f"{omitted_contract.count} of {n} entries have a blank Contract ID component in their ma-plan-id{omitted_contract.suffix()}"))
        checks.append((code_row_suffix("N3008"), "PASS" if not omitted_planid.count else "FAIL",
                        f"{omitted_planid.count} of {n} entries have a blank Plan ID component in their ma-plan-id{omitted_planid.suffix()}"))
        checks.append((code_row_suffix("N3007"), "PASS" if not omitted_segment.count else "FAIL",
                        f"{omitted_segment.count} of {n} entries have a blank Segment ID component in their ma-plan-id{omitted_segment.suffix()}"))
        if registry:
            checks.append((code_row_suffix("N3011"), "PASS" if not unknown_contract.count else "FAIL",
                            f"{unknown_contract.count} of {n} entries have a Contract ID not in {org}'s known-valid registry{unknown_contract.suffix()}"))
            checks.append((code_row_suffix("N3012"), "PASS" if not unknown_plan.count else "FAIL",
                            f"{unknown_plan.count} of {n} entries have a Plan ID not valid under that Contract ID in {org}'s registry{unknown_plan.suffix()}"))
            checks.append((code_row_suffix("N3013"), "PASS" if not unknown_segment.count else "FAIL",
                            f"{unknown_segment.count} of {n} entries have a Contract-Plan-Segment combination not in {org}'s known-valid registry{unknown_segment.suffix()}"))
        else:
            checks.append((code_row_suffix("N3011") + " / N3012 / N3013", "INFO",
                            f"No known-valid ID registry configured for org={org!r}; checks skipped"))
        checks.append((code_row_suffix("N3004"), "PASS" if not missing_year.count else "FAIL",
                        f"{missing_year.count} of {n} entries have no InsurancePlan.period.start / contract year{missing_year.suffix()}"))
        checks.append((code_row_suffix("P1013"), "PASS" if not bad_period_fmt.count else "FAIL",
                        f"{bad_period_fmt.count} of {n} entries have a period.start/end date not in YYYY-MM-DD format (e.g. DD-MM-YYYY is rejected){bad_period_fmt.suffix()}"))
        checks.append((code_row_suffix("F5001"), "PASS" if not missing_network.count and plan_ids else "FAIL",
                        f"{n} entries; plan IDs={plan_ids}; missing network link={missing_network.count}{missing_network.suffix()}"))
        ip_stale, ip_missing_dt = freshness_count(entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
        checks.append((code_row_suffix("C4010"), "FAIL" if ip_stale else "PASS",
                        f"{ip_stale} of {n} InsurancePlan records last updated more than {FRESHNESS_DAYS} days ago; {ip_missing_dt} have no lastUpdated"))
        ip_future_dt = future_date_bucket(resource_type, entries, lambda e: e.get("resource", {}).get("meta", {}).get("lastUpdated"))
        checks.append((code_row_suffix("P1014"), "PASS" if not ip_future_dt.count else "FAIL",
                        f"{ip_future_dt.count} of {n} entries have a meta.lastUpdated timestamp in the future{ip_future_dt.suffix()}"))
        checks.append(("contract year distribution", "INFO", f"years found: {dict(years)}"))

    else:
        checks.append(("resource type usage", "INFO", f"{n} entries of type {resource_type} -- not one of the 7 CMS-consumed types; CMS will ignore this data"))

    if resource_type == "Organization":
        coverage = {"total": n, "tested": fac, "not_tested_records": ntwk_records}
    else:
        coverage = {"total": n, "tested": n, "not_tested_records": []}
    return checks, coverage


def validate_machine_readable(providers):
    """Appendix A array-of-providers validator, one row per Appendix E code."""
    n = len(providers)
    missing_npi = Bucket(); bad_npi_fmt = Bucket()
    invalid_type = Bucket()
    missing_first = Bucket(); missing_last = Bucket(); missing_sex = Bucket(); missing_lang = Bucket()
    missing_facility_name = Bucket(); missing_facility_type = Bucket()
    no_plans = Bucket()
    missing_plan_id = Bucket(); malformed_plan_id = Bucket()
    missing_year = Bucket(); invalid_year = Bucket()
    missing_accepting = Bucket(); invalid_accepting = Bucket()
    missing_addresses = Bucket(); missing_city = Bucket(); missing_state = Bucket(); invalid_state = Bucket()
    missing_zip = Bucket(); invalid_zip = Bucket(); missing_street = Bucket(); missing_phone = Bucket(); invalid_phone = Bucket()
    missing_specialty = Bucket()
    invalid_lastupdated_fmt = Bucket(); future_lastupdated = Bucket()

    for p in providers:
        npi = p.get("npi")
        tag = f"NPI:{npi}" if npi else f"NPI:(missing) [{(p.get('name') or {}).get('last') or p.get('facilityName') or '?'}]"
        if not npi:
            missing_npi.hit(tag)
        elif not NPI_RE.match(str(npi)):
            bad_npi_fmt.hit(tag)

        ptype = (p.get("type") or "").strip()
        if ptype not in ("Individual", "Facility"):
            invalid_type.hit(tag)

        if ptype == "Individual":
            name = p.get("name") or {}
            if not name.get("first"):
                missing_first.hit(tag)
            if not name.get("last"):
                missing_last.hit(tag)
            if not p.get("sex"):
                missing_sex.hit(tag)
            if not p.get("languages"):
                missing_lang.hit(tag)
        elif ptype == "Facility":
            if not p.get("facilityName"):
                missing_facility_name.hit(tag)
            if not p.get("facilityType"):
                missing_facility_type.hit(tag)

        last_updated = p.get("lastUpdatedOn")
        if last_updated:
            if not DATE_RE.match(str(last_updated)):
                invalid_lastupdated_fmt.hit(tag)
            else:
                d = parse_iso_to_utc(last_updated)
                if d and d > datetime.now(timezone.utc):
                    future_lastupdated.hit(tag)

        plans = p.get("plans") or []
        if not plans:
            no_plans.hit(tag)
        for plan in plans:
            ma_plan_id = plan.get("maPlanId")
            if not ma_plan_id:
                missing_plan_id.hit(tag)
            elif not MAPLANID_RE.match(str(ma_plan_id)):
                malformed_plan_id.hit(f"{tag} maPlanId={ma_plan_id!r}")

            year = plan.get("year")
            if not year:
                missing_year.hit(tag)
            else:
                y = year[0] if isinstance(year, list) else year
                if not re.match(r"^\d{4}$", str(y)):
                    invalid_year.hit(f"{tag} year={y!r}")

            accepting = plan.get("accepting")
            if accepting is None or accepting == "":
                missing_accepting.hit(tag)
            elif accepting not in ("accepting", "not accepting"):
                invalid_accepting.hit(f"{tag} accepting={accepting!r}")

            addresses = plan.get("addresses") or []
            if not addresses:
                missing_addresses.hit(tag)
            for addr in addresses:
                if not addr.get("address"):
                    missing_street.hit(tag)
                if not addr.get("city"):
                    missing_city.hit(tag)
                state = addr.get("state")
                if not state:
                    missing_state.hit(tag)
                elif not state_valid(state):
                    invalid_state.hit(f"{tag} state={state!r}")
                zip_code = addr.get("zip")
                if not zip_code:
                    missing_zip.hit(tag)
                elif not zip_base_valid(zip_code):
                    invalid_zip.hit(f"{tag} zip={zip_code!r}")
                phone = addr.get("phone")
                if not phone:
                    missing_phone.hit(tag)
                elif not PHONE_RE.match(str(phone)):
                    invalid_phone.hit(f"{tag} phone={phone!r}")

            if not plan.get("specialty"):
                missing_specialty.hit(tag)

    stale, missing_dt = freshness_count(providers, lambda p: p.get("lastUpdatedOn"))

    def row(code, bucket, denom=n):
        return (code_row_suffix(code), "PASS" if not bucket.count else "FAIL",
                f"{bucket.count} of {denom} record(s) fail this check{bucket.suffix()}")

    checks = [
        row("P1001", missing_npi),
        ("NPI format (10 digits)", "PASS" if not bad_npi_fmt.count else "FAIL",
         f"{bad_npi_fmt.count} of {n} NPIs are not exactly 10 digits{bad_npi_fmt.suffix()}"),
        row("P1004", invalid_type),
        row("P1006", missing_first),
        row("P1007", missing_last),
        row("P1008", missing_facility_name),
        (code_row_suffix("P1004") + " (facilityType)", "PASS" if not missing_facility_type.count else "FAIL",
         f"{missing_facility_type.count} of {n} facility record(s) have no facilityType{missing_facility_type.suffix()}"),
        row("P1009", missing_specialty),
        row("P1010", missing_sex),
        row("P1011", missing_lang),
        row("P1016", no_plans),
        row("N3001", missing_plan_id),
        row("N3002", malformed_plan_id),
        row("N3004", missing_year),
        row("N3005", invalid_year),
        row("P1012", missing_accepting),
        row("P1018", invalid_accepting),
        row("A2001", missing_addresses),
        row("A2007", missing_street),
        row("A2002", missing_city),
        row("A2003", missing_state),
        row("A2004", invalid_state),
        row("A2005", missing_zip),
        row("A2006", invalid_zip),
        row("A2009", missing_phone),
        row("A2010", invalid_phone),
        row("P1013", invalid_lastupdated_fmt),
        row("P1014", future_lastupdated),
        (code_row_suffix("C4010"), "FAIL" if stale else "PASS",
         f"{stale} of {n} providers last updated more than {FRESHNESS_DAYS} days ago; {missing_dt} have no lastUpdatedOn"),
    ]
    return checks, {"total": n, "tested": n, "not_tested_records": []}


def cross_checks(org, contract, index_url, index_doc, bundles, mr_providers):
    """Returns list of (role, resource_info, check_name, result, detail) tuples."""
    out = []
    url_year = extract_year_from_url(index_url)
    index_year = str(index_doc.get("contract_year") or "").strip() or None

    fhir_years = set()
    for e in bundles.get("InsurancePlan", []):
        r = e.get("resource", {})
        yr = (r.get("period", {}) or {}).get("start", "")[:4]
        if yr:
            fhir_years.add(yr)
    mr_years = set()
    for p in mr_providers:
        for plan in p.get("plans", []) or []:
            for yr in plan.get("year", []) or []:
                mr_years.add(str(yr)[:4])

    data_years = fhir_years or mr_years
    if url_year and data_years:
        mismatched = data_years - {url_year}
        result = "FAIL" if mismatched else "PASS"
        detail = (f"URL path year={url_year}, index.json contract_year={index_year}, "
                  f"contract year(s) found in data={sorted(data_years)}")
        if mismatched:
            detail += f" -- MISMATCH: data reports year(s) {sorted(mismatched)} but is hosted/declared as {url_year}"
        out.append(("(cross-file)", "contract year vs data",
                    code_row_suffix("N3005") + " / contract-year consistency", result, detail))
    elif url_year and not data_years:
        out.append(("(cross-file)", "contract year vs data",
                    code_row_suffix("N3005") + " / contract-year consistency", "INFO",
                    "No InsurancePlan / plans[].year data found to compare against the URL year"))

    fhir_plan_ids = []
    for e in bundles.get("InsurancePlan", []):
        r = e.get("resource", {})
        pid = get_identifier(r, CMS_PLAN_SYS)
        if pid and MAPLANID_RE.match(pid):
            fhir_plan_ids.append(pid)
    if fhir_plan_ids:
        mismatched_pids = [p for p in fhir_plan_ids if p.split("-")[0] != contract]
        out.append(("(cross-file)", f"{len(fhir_plan_ids)} InsurancePlan ID(s) checked",
                    code_row_suffix("N3014") + " / InsurancePlan contract match", "FAIL" if mismatched_pids else "PASS",
                    f"plan IDs={fhir_plan_ids}" + (f" -- MISMATCH: {mismatched_pids} do not start with submitting contract {contract}"
                                                    if mismatched_pids else f" -- all match submitting contract {contract}")))

    bad_locations = {}
    for e in bundles.get("Location", []):
        r = e.get("resource", {})
        addr = r.get("address", {}) or {}
        missing = [name for name, val in (
            ("line", addr.get("line")),
            ("city", addr.get("city")),
            ("state", addr.get("state")),
            ("postalCode", addr.get("postalCode")),
        ) if not val]
        if missing:
            bad_locations[str(r.get("id"))] = missing

    if bad_locations:
        def ref_id(ref):
            return str(ref or "").split("/")[-1]

        dangling = []
        for rtype in ("PractitionerRole", "OrganizationAffiliation"):
            for e in bundles.get(rtype, []):
                r = e.get("resource", {})
                src_id = str(r.get("id"))
                for loc in r.get("location", []) or []:
                    lid = ref_id(loc.get("reference"))
                    if lid in bad_locations:
                        dangling.append((rtype, src_id, lid))

        loc_file = f"incomplete_locations_{contract}.csv"
        with open(loc_file, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Location ID", "Missing Fields"])
            for lid, miss in sorted(bad_locations.items()):
                w.writerow([lid, ", ".join(miss)])

        ref_file = f"dangling_references_{contract}.csv"
        with open(ref_file, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Referencing Resource Type", "Referencing Resource ID",
                        "Referenced Location ID", "Missing Fields on Location"])
            for rtype, src_id, lid in dangling:
                w.writerow([rtype, src_id, lid, ", ".join(bad_locations[lid])])

        pr_hits = sum(1 for d in dangling if d[0] == "PractitionerRole")
        oa_hits = sum(1 for d in dangling if d[0] == "OrganizationAffiliation")
        pr_total = len(bundles.get("PractitionerRole", []))
        oa_total = len(bundles.get("OrganizationAffiliation", []))
        combined_total = pr_total + oa_total
        out.append(("(cross-file)",
                    f"Bundle/PractitionerRole/{pr_total} entries; Bundle/OrganizationAffiliation/{oa_total} entries",
                    code_row_suffix("F5008") + " / dangling location references", "FAIL" if dangling else "PASS",
                    f"{pr_hits + oa_hits} of {combined_total} PractitionerRole/OrganizationAffiliation entries "
                    f"reference a Location with an incomplete/empty address ({pr_hits} PractitionerRole, "
                    f"{oa_hits} OrganizationAffiliation); full ID lists written to {loc_file} and {ref_file}"))

    return out


CODE_APPLICABLE_RESOURCE_TYPES = {
    "A2001": {"Practitioner", "Location", "Organization"},
    "A2002": {"Practitioner", "Location", "Organization"},
    "A2003": {"Practitioner", "Location", "Organization"},
    "A2004": {"Practitioner", "Location", "Organization"},
    "A2005": {"Practitioner", "Location", "Organization"},
    "A2006": {"Practitioner", "Location", "Organization"},
    "A2007": {"Practitioner", "Location", "Organization"},
    "A2009": {"Practitioner", "PractitionerRole", "Location", "Organization", "OrganizationAffiliation"},
    "A2010": {"Practitioner", "PractitionerRole", "Location", "Organization", "OrganizationAffiliation"},
    "C4010": {"Practitioner", "PractitionerRole", "Location", "Organization", "OrganizationAffiliation", "InsurancePlan"},
    "F5001": {"PractitionerRole", "OrganizationAffiliation", "InsurancePlan"},
    "F5002": {"OrganizationAffiliation"},
    "F5003": {"PractitionerRole"},
    "F5004": {"PractitionerRole", "OrganizationAffiliation"},
    "F5009": {"Practitioner", "PractitionerRole", "Organization"},
    "N3001": {"InsurancePlan"}, "N3002": {"InsurancePlan"}, "N3004": {"InsurancePlan"},
    "N3006": {"InsurancePlan"}, "N3007": {"InsurancePlan"}, "N3008": {"InsurancePlan"},
    "N3011": {"InsurancePlan"}, "N3012": {"InsurancePlan"}, "N3013": {"InsurancePlan"},
    "P1001": {"Practitioner", "Organization"},
    "P1004": {"Organization"},
    "P1006": {"Practitioner"}, "P1007": {"Practitioner"}, "P1010": {"Practitioner"}, "P1011": {"Practitioner"},
    "P1008": {"Organization"},
    "P1009": {"Practitioner", "PractitionerRole", "OrganizationAffiliation"},
    "P1012": {"PractitionerRole"},
    "P1013": {"Practitioner", "PractitionerRole", "Location", "Organization", "OrganizationAffiliation", "InsurancePlan"},
    "P1014": {"Practitioner", "PractitionerRole", "Location", "Organization", "OrganizationAffiliation", "InsurancePlan"},
    "P1016": {"PractitionerRole", "OrganizationAffiliation"},
    "P1018": {"PractitionerRole"},
}


PLACEHOLDER_TYPE_TO_CODE = {
    "Location": "F5008",
    "Practitioner": "F5007",
    "Organization": "F5006",
    "InsurancePlan": "F5005",
}


def placeholder_checks(bundles):
    """Flags any resource carrying the HAPI FHIR resource-placeholder extension."""
    out = []
    per_type = {}
    total = 0
    for rtype, entries in bundles.items():
        ids = [e.get("resource", {}).get("id") for e in entries if is_placeholder_resource(e.get("resource", {}))]
        if ids:
            per_type[rtype] = ids
            total += len(ids)

    for rtype, code in PLACEHOLDER_TYPE_TO_CODE.items():
        rtype_total = len(bundles.get(rtype, []))
        resource_info = f"Bundle/{rtype}/{rtype_total} entries"
        if rtype in per_type:
            ids = per_type[rtype]
            id_preview = ', '.join(str(i) for i in ids[:10]) + ('...' if len(ids) > 10 else '')
            out.append(("(cross-file)", resource_info,
                        code_row_suffix(code) + f" / {rtype} placeholder(s)", "FAIL",
                        f"{len(ids)} of {rtype_total} referenced {rtype} resource(s) are placeholders "
                        f"(referenced but never actually included in the export) -- ids: {id_preview}"))
        else:
            out.append(("(cross-file)", resource_info,
                        code_row_suffix(code) + f" / {rtype} placeholder(s)", "PASS",
                        f"0 of {rtype_total} referenced {rtype} resource(s) are placeholders -- "
                        f"no hapifhir.io resource-placeholder extension found on any {rtype} reference"))
    return out


def resource_identifier(rtype, r):
    """Business identifier for a resource: NPI for providers/facilities,
    MA plan ID for InsurancePlan, else the first identifier value found."""
    if rtype in ("Practitioner", "PractitionerRole", "Organization"):
        npi = get_identifier(r, NPI_SYS)
        if npi:
            return f"NPI:{npi}"
    if rtype == "InsurancePlan":
        pid = get_identifier(r, CMS_PLAN_SYS)
        if pid:
            return f"maPlanId:{pid}"
    for ident in r.get("identifier", []) or []:
        if ident.get("value"):
            return str(ident.get("value"))
    return ""


def all_identifiers(r):
    parts = []
    for ident in r.get("identifier", []) or []:
        sys_ = ident.get("system") or ""
        val = ident.get("value") or ""
        parts.append(f"{sys_}={val}" if sys_ else val)
    return "; ".join(parts)


def meta_source(r):
    return (r.get("meta", {}) or {}).get("source", "") or ""


def resource_name(rtype, r):
    if rtype in ("Practitioner", "PractitionerRole"):
        n = (r.get("name") or [{}])
        n = n[0] if n else {}
        full = " ".join((n.get("given") or []) + ([n.get("family")] if n.get("family") else [])).strip()
        return full or n.get("text", "") or ""
    return r.get("name", "") or ""


def _identifier_systems(r):
    sys_ = [i.get("system") or "(no system)" for i in r.get("identifier", []) or []]
    return ", ".join(sys_) if sys_ else "no identifier element"


def field_issues(rtype, r, ctx=None, url_year=None, org=None):
    """Return a list of (field, error, expected, actual, code) issue tuples for one resource."""
    ctx = ctx or EMPTY_CTX

    def _date_fmt_issues(field_label, resource_field_path, value, code, check_future=True):
        rows = []
        if value and not DATE_RE.match(str(value)[:10]):
            rows.append((field_label, f"{resource_field_path} is not in YYYY-MM-DD format (e.g. DD-MM-YYYY is rejected)",
                         "YYYY-MM-DD (ISO date, year first)", f"value={value!r}", code))
        if value and check_future:
            d = parse_iso_to_utc(value)
            if d and d > datetime.now(timezone.utc):
                rows.append((field_label, f"{resource_field_path} is a future-dated timestamp",
                             "a timestamp not in the future", f"value={value!r}", "P1014"))
        return rows

    out = []
    if rtype == "Practitioner":
        rid = str(r.get("id"))
        npi = get_identifier(r, NPI_SYS)
        if not npi and rid not in ctx["role_npi_prac_ids"]:
            out.append(("npi", "No NPI identifier under the us-npi system on the Practitioner or a linked PractitionerRole",
                        f"identifier with system={NPI_SYS} and a 10-digit value",
                        f"systems present: {_identifier_systems(r)}", "P1001"))
        elif npi and not NPI_RE.match(str(npi)):
            out.append(("npi", "NPI is not exactly 10 digits", "10-digit numeric NPI", f"value={npi}", "P1001"))
        name = (r.get("name") or [{}])[0]
        if not name.get("family"):
            out.append(("name.family", "Practitioner last/family name missing",
                        "Practitioner.name[0].family present", "family empty/absent", "P1007"))
        if not name.get("given"):
            out.append(("name.given", "Practitioner first/given name missing",
                        "Practitioner.name[0].given[0] present", "given empty/absent", "P1006"))
        phone_entry = next((t for t in r.get("telecom", []) or [] if t.get("system") == "phone"), None)
        if not phone_entry and rid not in ctx["role_phone_prac_ids"]:
            present = ", ".join(sorted({t.get("system") or "(none)" for t in r.get("telecom", []) or []})) or "no telecom"
            out.append(("phone", "No phone telecom entry on the Practitioner or a linked PractitionerRole",
                        "telecom entry with system=phone (Practitioner or PractitionerRole, Appendix B 7a/7b)",
                        f"telecom systems present: {present}", "A2009"))
        elif phone_entry:
            phone_val = str(phone_entry.get("value") or "").strip()
            if not PHONE_RE.match(phone_val):
                out.append(("phone", "Practitioner phone number does not match 10-digit format",
                            "10-digit numeric phone number", f"value={phone_entry.get('value')!r}", "A2010"))
        if not r.get("gender"):
            out.append(("gender", "No gender element", "Practitioner.gender present", "absent", "P1010"))
        if not r.get("communication"):
            out.append(("language", "No language/communication element",
                        "Practitioner.communication with language code(s)", "communication absent", "P1011"))
        if not any((q.get("code") or {}).get("coding") for q in r.get("qualification", []) or []) \
                and rid not in ctx["role_spec_prac_ids"]:
            out.append(("specialty", "No taxonomy on Practitioner.qualification and no specialty on the linked PractitionerRole",
                        "Practitioner.qualification.code (taxonomy) or PractitionerRole.specialty",
                        "qualification and linked PractitionerRole.specialty both absent", "P1009"))
        linked_loc_ids = ctx["prac_loc_ids"].get(rid, set())
        linked_locs = [ctx["loc_by_id"][lid] for lid in linked_loc_ids if lid in ctx["loc_by_id"]]
        locs_with_addr = [l for l in linked_locs if (l.get("address") or {})]
        if not locs_with_addr:
            out.append(("address", "No address resolvable via any linked PractitionerRole -> Location",
                        "an address on at least one linked Location",
                        f"{len(linked_locs)} linked Location(s), none with an address", "A2001"))
        else:
            addr = locs_with_addr[0].get("address") or {}
            addr_field_codes = {"line": "A2007", "city": "A2002", "state": "A2003", "postalCode": "A2005"}
            for fld, code in addr_field_codes.items():
                if not addr.get(fld):
                    out.append((f"address.{fld}", f"Resolved Location address.{fld} missing",
                                f"Location.address.{fld} present on the linked Location", "empty/absent", code))
            state = addr.get("state")
            if state and not state_valid(state):
                out.append(("address.state", "Resolved Location address.state is not a 2-letter abbreviation",
                            "Two letter state abbreviation (e.g., PA)", f"value={state!r}", "A2004"))
            zip_code = addr.get("postalCode")
            if zip_code and not zip_base_valid(zip_code):
                out.append(("address.postalCode", "Resolved Location address.postalCode does not match 5-digit or ZIP+4 format",
                            "5-digit zip or ZIP+4 (e.g. 19107 or 19107-4108)", f"value={zip_code!r}", "A2006"))
        npis = [i.get("value") for i in r.get("identifier", []) or [] if i.get("system") == NPI_SYS]
        if len(npis) > 1:
            out.append(("npi", "More than one us-npi identifier on this resource",
                        "at most one identifier with system=us-npi", f"{len(npis)} present: {npis}", "F5009"))
        out.extend(_date_fmt_issues("meta.lastUpdated", "Practitioner.meta.lastUpdated", r.get("meta", {}).get("lastUpdated"), "P1013"))
    elif rtype == "PractitionerRole":
        rid = str(r.get("id"))
        if not r.get("meta", {}).get("lastUpdated"):
            out.append(("lastUpdated", "meta.lastUpdated missing", "PractitionerRole.meta.lastUpdated present", "absent", "C4010"))
        if not r.get("practitioner"):
            out.append(("practitioner", "No Practitioner reference", "PractitionerRole.practitioner referencing a Practitioner", "absent", "F5003"))
        if not r.get("location"):
            out.append(("location", "No Location reference", "PractitionerRole.location referencing a Location", "absent", "F5004"))
        prac_id = _ref_id((r.get("practitioner") or {}).get("reference"))
        if not any(c.get("code") for s in r.get("specialty", []) or [] for c in s.get("coding", []) or []) \
                and prac_id not in ctx["prac_qualification_ids"]:
            out.append(("specialty", "No specialty code on PractitionerRole and no taxonomy on the linked Practitioner",
                        "specialty.coding.code (NUCC) or the linked Practitioner.qualification",
                        "absent/empty", "P1009"))
        if not any("network" in (ext.get("url") or "").lower() for ext in r.get("extension", []) or []):
            out.append(("network", "No network-reference extension",
                        "network-reference extension linking to InsurancePlan", "absent", "F5001"))
        phone_entry = next((t for t in r.get("telecom", []) or [] if t.get("system") == "phone"), None)
        if not phone_entry and prac_id not in ctx["prac_own_phone_ids"]:
            out.append(("phone", "No phone on PractitionerRole.telecom and none on the linked Practitioner",
                        "telecom entry with system=phone (PractitionerRole or Practitioner, Appendix B 7a/7b)",
                        "absent on both", "A2009"))
        elif phone_entry:
            phone_val = str(phone_entry.get("value") or "").strip()
            if not PHONE_RE.match(phone_val):
                out.append(("phone", "PractitionerRole phone does not match 10-digit format",
                            "10-digit numeric phone number", f"value={phone_entry.get('value')!r}", "A2010"))
        newpt_ext = next((ext for ext in r.get("extension", []) or []
                          if "newpatients" in (ext.get("url") or "").lower()), None)
        if not newpt_ext:
            out.append(("acceptingPatients", "No accepting-new-patients extension",
                        "the newpatients extension with a valid code", "absent", "P1012"))
        else:
            VALID_NEWPT_CODES = {"nopt", "newpt", "existptonly", "existptfam"}
            codes = [c.get("code") for sub in newpt_ext.get("extension", []) or []
                     for c in [sub.get("valueCodeableConcept", {}) or {}]
                     for c in c.get("coding", []) or []]
            if not codes or not any(c in VALID_NEWPT_CODES for c in codes):
                out.append(("acceptingPatients", "Accepting-new-patients code not in the valid set",
                            "one of {nopt, newpt, existptonly, existptfam}", f"codes={codes}", "P1018"))
        network_id_sys = NETWORK_IDENTIFIER_SYSTEM_BY_ORG.get(org)
        if network_id_sys:
            network_ext = next((ext for ext in r.get("extension", []) or []
                                if "network" in (ext.get("url") or "").lower()), None)
            network_ref = (network_ext or {}).get("valueReference")
            if not _reference_has_identifier(network_ref, network_id_sys):
                out.append(("network", f"No identifier under {network_id_sys} on the network reference",
                            f"network reference identifier.system={network_id_sys}", "absent/mismatched", "P1016"))
        npis = [i.get("value") for i in r.get("identifier", []) or [] if i.get("system") == NPI_SYS]
        if len(npis) > 1:
            out.append(("npi", "More than one us-npi identifier on this resource",
                        "at most one identifier with system=us-npi", f"{len(npis)} present: {npis}", "F5009"))
        out.extend(_date_fmt_issues("meta.lastUpdated", "PractitionerRole.meta.lastUpdated", r.get("meta", {}).get("lastUpdated"), "P1013"))
    elif rtype == "Location":
        addr = r.get("address", {}) or {}
        if not addr:
            out.append(("address", "Location has no address element at all",
                        "Location.address present", "absent", "A2001"))
        state = addr.get("state")
        if not state:
            out.append(("address.state", "Location address.state missing",
                        "Location.address.state present",
                        "no address element" if not addr else "address present but state empty", "A2003"))
        elif not state_valid(state):
            out.append(("address.state", "Location address.state is not a 2-letter abbreviation",
                        "Two letter state abbreviation (e.g., PA)", f"value={state!r}", "A2004"))
        if not _location_excluded_by_physical_type(r):
            field_codes = {"line": "A2007", "city": "A2002", "postalCode": "A2005"}
            for fld, code in field_codes.items():
                if not addr.get(fld):
                    out.append((f"address.{fld}", f"Location address.{fld} missing",
                                f"Location.address.{fld} present",
                                "no address element" if not addr else f"address present but {fld} empty", code))
            zip_code = addr.get("postalCode")
            if zip_code and not zip_base_valid(zip_code):
                out.append(("address.postalCode", "Location address.postalCode does not match 5-digit or ZIP+4 format",
                            "5-digit zip or ZIP+4 (e.g. 19107 or 19107-4108)", f"value={zip_code!r}", "A2006"))
            phone = _phone_value(r)
            if not phone:
                out.append(("phone", "No phone on Location.telecom",
                            "telecom entry with system=phone", "absent", "A2009"))
            else:
                phone_digits = str(phone).strip()
                if not PHONE_RE.match(phone_digits):
                    out.append(("phone", "Location phone does not match 10-digit format",
                                "10-digit numeric phone number", f"value={phone!r}", "A2010"))
        period = r.get("period") or {}
        out.extend(_date_fmt_issues("meta.lastUpdated", "Location.meta.lastUpdated", r.get("meta", {}).get("lastUpdated"), "P1013"))
        out.extend(_date_fmt_issues("period.start", "Location.period.start", period.get("start"), "P1013"))
        out.extend(_date_fmt_issues("period.end", "Location.period.end", period.get("end"), "P1013"))
    elif rtype == "Organization":
        rid = str(r.get("id"))
        codings = [c for t in r.get("type", []) or [] for c in t.get("coding", []) or []]
        codes = [c.get("code") for c in codings]
        if "ntwk" in codes:
            net_phone = _phone_value(r)
            if net_phone:
                net_phone_digits = str(net_phone).strip()
                if not PHONE_RE.match(net_phone_digits):
                    out.append(("phone", "Network Organization phone does not match 10-digit format",
                                "10-digit numeric phone number", f"value={net_phone!r}", "A2010"))
            return out
        if "fac" not in codes:
            out.append(("facilityType", "Facility type code 'fac' missing",
                        f"Organization.type.coding.code='fac' with system={ORGTYPE_SYS}",
                        f"type codes present: {', '.join(c for c in codes if c) or 'none'}", "P1004"))
        elif not any(c.get("code") == "fac" and c.get("system") == ORGTYPE_SYS for c in codings):
            out.append(("facilityType", "'fac' type code present but not under the OrgTypeCS system",
                        f"Organization.type.coding with code='fac' AND system={ORGTYPE_SYS}",
                        f"'fac' coding system(s): {', '.join(sorted({c.get('system') or '(no system)' for c in codings if c.get('code') == 'fac'}))}",
                        "P1004"))
        npi = get_identifier(r, NPI_SYS)
        if not npi:
            out.append(("npi", "No NPI identifier under the us-npi system",
                        f"identifier with system={NPI_SYS} and a 10-digit value",
                        f"systems present: {_identifier_systems(r)}", "P1001"))
        elif not NPI_RE.match(str(npi)):
            out.append(("npi", "NPI is not exactly 10 digits", "10-digit numeric NPI", f"value={npi}", "P1001"))
        npis = [i.get("value") for i in r.get("identifier", []) or [] if i.get("system") == NPI_SYS]
        if len(npis) > 1:
            out.append(("npi", "More than one us-npi identifier on this resource",
                        "at most one identifier with system=us-npi", f"{len(npis)} present: {npis}", "F5009"))
        if not r.get("name"):
            out.append(("name", "Facility name missing", "Organization.name present", "absent", "P1008"))
        fallback_locs = _org_fallback_locations(ctx, rid)
        org_addrs = r.get("address") or []
        if isinstance(org_addrs, dict):
            org_addrs = [org_addrs]
        if not org_addrs and not any((l.get("address") or {}) for l in fallback_locs):
            out.append(("address", "Facility address missing on Organization AND no linked Location address",
                        "Organization.address or Location.address via OrganizationAffiliation (Appendix B 7a/7b)",
                        f"no Organization.address; {len(fallback_locs)} linked Location(s), none with an address", "A2001"))
        addr_field_codes = {"line": "A2007", "city": "A2002", "state": "A2003", "postalCode": "A2005"}
        for addr in org_addrs:
            for fld, code in addr_field_codes.items():
                if not addr.get(fld):
                    out.append((f"address.{fld}", f"Organization.address.{fld} missing",
                                f"Organization.address.{fld} present", "empty/absent", code))
            state = addr.get("state")
            if state and not state_valid(state):
                out.append(("address.state", "Organization.address.state is not a 2-letter abbreviation",
                            "Two letter state abbreviation (e.g., PA)", f"value={state!r}", "A2004"))
            zip_code = addr.get("postalCode")
            if zip_code and not zip_base_valid(zip_code):
                out.append(("address.postalCode", "Organization.address.postalCode does not match 5-digit or ZIP+4 format",
                            "5-digit zip or ZIP+4 (e.g. 19107 or 19107-4108)", f"value={zip_code!r}", "A2006"))
        phone = _phone_value(r)
        if not phone and not any(str(l.get("id")) in ctx["loc_phone_ids"] for l in fallback_locs):
            out.append(("phone", "Facility phone missing on Organization AND on any linked Location",
                        "Organization.telecom[system=phone] or Location.telecom[system=phone] (Appendix B 8a/8b)",
                        f"no phone telecom; {len(fallback_locs)} linked Location(s), none with a phone", "A2009"))
        elif phone:
            phone_digits = str(phone).strip()
            if not PHONE_RE.match(phone_digits):
                out.append(("phone", "Organization phone does not match 10-digit format",
                            "10-digit numeric phone number", f"value={phone!r}", "A2010"))
        period = r.get("period") or {}
        out.extend(_date_fmt_issues("meta.lastUpdated", "Organization.meta.lastUpdated", r.get("meta", {}).get("lastUpdated"), "P1013"))
        out.extend(_date_fmt_issues("period.start", "Organization.period.start", period.get("start"), "P1013"))
        out.extend(_date_fmt_issues("period.end", "Organization.period.end", period.get("end"), "P1013"))
    elif rtype == "OrganizationAffiliation":
        if not r.get("network"):
            out.append(("network", "No network reference", "OrganizationAffiliation.network linking to InsurancePlan", "absent", "F5001"))
        if not r.get("organization"):
            out.append(("organization", "No organization reference", "OrganizationAffiliation.organization linking to Organization", "absent", "F5002"))
        if not r.get("location"):
            out.append(("location", "No location reference", "OrganizationAffiliation.location linking to Location", "absent", "F5004"))
        if not any(c.get("code") for s in r.get("specialty", []) or [] for c in s.get("coding", []) or []):
            out.append(("specialty", "No specialty code", "specialty.coding.code (NUCC) present", "absent/empty", "P1009"))
        network_id_sys = NETWORK_IDENTIFIER_SYSTEM_BY_ORG.get(org)
        if network_id_sys and not any(_reference_has_identifier(ref, network_id_sys) for ref in r.get("network", []) or []):
            out.append(("network", f"No identifier under {network_id_sys} on any network reference",
                        f"a network reference with identifier.system={network_id_sys}", "absent/mismatched", "P1016"))
        phone = _phone_value(r)
        if not phone:
            out.append(("phone", "No phone on OrganizationAffiliation.telecom",
                        "telecom entry with system=phone", "absent", "A2009"))
        else:
            phone_digits = str(phone).strip()
            if not PHONE_RE.match(phone_digits):
                out.append(("phone", "OrganizationAffiliation phone does not match 10-digit format",
                            "10-digit numeric phone number", f"value={phone!r}", "A2010"))
        if not r.get("meta", {}).get("lastUpdated"):
            out.append(("lastUpdated", "meta.lastUpdated missing", "OrganizationAffiliation.meta.lastUpdated present", "absent", "C4010"))
        out.extend(_date_fmt_issues("meta.lastUpdated", "OrganizationAffiliation.meta.lastUpdated", r.get("meta", {}).get("lastUpdated"), "P1013"))
    elif rtype == "InsurancePlan":
        pid = get_identifier(r, CMS_PLAN_SYS)
        if not pid:
            out.append(("maPlanId", "No MA plan identifier under the CMS ma-plan-id system",
                        f"identifier system={CMS_PLAN_SYS}, format H####-###-###",
                        f"systems present: {_identifier_systems(r)}", "N3001"))
        elif not MAPLANID_RE.match(str(pid)):
            out.append(("maPlanId", "MA plan identifier does not match H####-###-### format",
                        "format H####-###-### (use 000 for an unsegmented plan, not blank)",
                        f"value={pid}", "N3002"))
        if pid:
            contract_part, planid_part, segment_part = _parse_ma_plan_id(pid)
            if not contract_part:
                out.append(("maPlanId", "ma-plan-id has a blank Contract ID component",
                            "a non-blank Contract ID segment (e.g. 'H5826' in H5826-014-000)", f"value={pid!r}", "N3006"))
            if not planid_part:
                out.append(("maPlanId", "ma-plan-id has a blank Plan ID component",
                            "a non-blank Plan ID segment (e.g. '014' in H5826-014-000)", f"value={pid!r}", "N3008"))
            if not segment_part:
                out.append(("maPlanId", "ma-plan-id has a blank Segment ID component",
                            "a non-blank Segment ID segment (e.g. '000' in H5826-014-000)", f"value={pid!r}", "N3007"))
            registry = _known_id_registry(org)
            if registry and MAPLANID_RE.match(pid):
                if contract_part not in registry["contracts"]:
                    out.append(("maPlanId", f"Contract ID not in {org}'s known-valid registry",
                                "a Contract ID CMS actually issued to this org", f"value={pid!r}", "N3011"))
                elif (contract_part, planid_part) not in registry["plans"]:
                    out.append(("maPlanId", f"Plan ID not valid under that Contract ID in {org}'s registry",
                                "a Plan ID valid under this Contract ID", f"value={pid!r}", "N3012"))
                elif pid not in registry["full"]:
                    out.append(("maPlanId", f"Contract-Plan-Segment combination not in {org}'s known-valid registry",
                                "a Contract-Plan-Segment combination that actually exists", f"value={pid!r}", "N3013"))
        period = r.get("period", {}) or {}
        period_start = period.get("start")
        if not period_start:
            out.append(("period/contract-year", "InsurancePlan.period missing",
                        "InsurancePlan.period.start containing the contract year", "absent", "N3004"))
        else:
            if url_year:
                data_year = str(period_start)[:4]
                if data_year != url_year:
                    out.append(("period/contract-year", "InsurancePlan.period.start year does not match the hosting URL's contract year",
                                f"period.start year = {url_year} (matching the /{url_year}/ URL path)",
                                f"period.start={period_start} (year {data_year})", "N3005"))
            out.extend(_date_fmt_issues("period.start", "InsurancePlan.period.start", period_start, "P1013", check_future=False))
        out.extend(_date_fmt_issues("period.end", "InsurancePlan.period.end", period.get("end"), "P1013", check_future=False))
        out.extend(_date_fmt_issues("meta.lastUpdated", "InsurancePlan.meta.lastUpdated", r.get("meta", {}).get("lastUpdated"), "P1013"))
        if not r.get("network"):
            out.append(("network", "InsurancePlan.network missing", "InsurancePlan.network present", "absent", "F5001"))
    return out


_CATEGORY_LABELS = {
    "practitioner": "Practitioner",
    "practitionerrole": "PractitionerRole",
    "organization": "Organization",
    "organizationaffiliation": "OrganizationAffiliation",
    "location": "Location",
    "insuranceplan": "InsurancePlan",
    "network": "Network",
    "healthcareservice": "HealthcareService",
}


def write_resource_file_summary(org, contract, file_summary):
    fname = f"resource_file_summary_{contract}.csv"
    out_rows = []
    for cat, info in sorted(file_summary.items()):
        label = _CATEGORY_LABELS.get(cat.lower(), cat.replace("_", " ").title().replace(" ", ""))
        file_details = "; ".join(info["files"])
        indexed = "skipped (not MPF-consumed, not downloaded)" if info["skipped"] else info["indexed"]
        out_rows.append((label, len(info["files"]), file_details, indexed))

    with open(fname, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Resource Type", "JSON Files Processed", "File Details", "Total Indexed Count"])
        for label, file_count, file_details, indexed in out_rows:
            w.writerow([label, file_count, file_details, indexed])

    TYPE_W, FILES_W, INDEXED_W = 24, 6, 20
    print(f"\n  Resource File Processing Summary ({org} {contract}):")
    print(f"  {'Resource Type':<{TYPE_W}} {'Files':>{FILES_W}}  {'Total Indexed Count':>{INDEXED_W}}  File Details")
    for label, file_count, file_details, indexed in out_rows:
        indexed_str = "skipped" if indexed == "skipped (not MPF-consumed, not downloaded)" else str(indexed)
        print(f"  {label:<{TYPE_W}} {file_count:>{FILES_W}}  {indexed_str:>{INDEXED_W}}  {file_details}")
    print(f"  -> written to {fname}")

    return fname, out_rows


def write_placeholder_details(contract, bundles):
    fname = f"placeholders_{contract}.csv"
    n = 0
    code_examples = {}
    with open(fname, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Resource Type", "Resource ID", "Identifier", "All Identifiers (system=value)", "Source (meta.source)", "Name"])
        for rtype, entries in bundles.items():
            for e in entries:
                r = e.get("resource", {})
                if is_placeholder_resource(r):
                    ident = resource_identifier(rtype, r)
                    smile = meta_source(r)
                    w.writerow([rtype, str(r.get("id")), ident,
                                all_identifiers(r), smile, resource_name(rtype, r)])
                    n += 1
                    code = PLACEHOLDER_TYPE_TO_CODE.get(rtype)
                    if code and code not in code_examples:
                        code_examples[code] = (rtype, ident or f"id:{r.get('id')}", smile,
                                                f"a real {rtype} resource supplied in the export",
                                                f"{rtype}/{r.get('id')} is referenced but was never actually included (HAPI placeholder stub)")
    return fname, n, code_examples


def write_phone_details(org, contract, bundles):
    fname = f"phone_number_issues_{contract}.csv"
    missing_only_fname = f"missing_phone_numbers_{contract}.csv"
    n = 0
    missing_n = 0
    code_examples = {}
    ctx = build_fhir_context(bundles)
    header = ["Resource Type", "Resource ID", "Identifier", "All Identifiers (system=value)",
              "Source (meta.source)", "Name", "Error", "Expected", "Actual", "Error Code"]
    with open(fname, "w", newline="", encoding="utf-8") as f, \
         open(missing_only_fname, "w", newline="", encoding="utf-8") as fm:
        w = csv.writer(f)
        wm = csv.writer(fm)
        w.writerow(header)
        wm.writerow(header)
        for rtype, entries in bundles.items():
            for e in entries:
                r = e.get("resource", {})
                if is_placeholder_resource(r):
                    continue
                for field, error, expected, actual, code in field_issues(rtype, r, ctx, org=org):
                    if field != "phone" or code not in ("A2009", "A2010"):
                        continue
                    ident = resource_identifier(rtype, r)
                    row = [rtype, str(r.get("id")), ident,
                           all_identifiers(r), meta_source(r), resource_name(rtype, r),
                           error, expected, actual, code]
                    w.writerow(row)
                    n += 1
                    if code == "A2009":
                        wm.writerow(row)
                        missing_n += 1
                    if code not in code_examples:
                        code_examples[code] = (rtype, ident or f"id:{r.get('id')}", meta_source(r), expected, actual)
    return fname, n, code_examples, missing_only_fname, missing_n


_ALL_SAME_DIGIT_RE = re.compile(r"^(\d)\1{9}$")
_LONG_REPEAT_RUN_RE = re.compile(r"(\d)\1{6,}")
_SEQUENTIAL_PHONES = {"1234567890", "0123456789", "0987654321", "9876543210"}


def _is_suspicious_placeholder_phone(digits):
    if _ALL_SAME_DIGIT_RE.match(digits):
        return True
    if digits in _SEQUENTIAL_PHONES:
        return True
    if _LONG_REPEAT_RUN_RE.search(digits):
        return True
    return False


_PHONE_DQ_RESOURCE_TYPES = ("Practitioner", "PractitionerRole", "Location",
                            "Organization", "OrganizationAffiliation")


def write_phone_data_quality_report(org, contract, bundles):
    fname = f"phone_data_quality_issues_{contract}.csv"
    header = ["Organization", "Contract", "Resource Type", "Resource ID", "Identifier",
              "All Identifiers (system=value)", "Source (meta.source)", "Name",
              "Error Code", "Field", "Error", "Expected", "Actual", "Result"]
    n = 0
    with open(fname, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        rows_out = []
        for rtype in _PHONE_DQ_RESOURCE_TYPES:
            for e in bundles.get(rtype, []):
                r = e.get("resource", {})
                if is_placeholder_resource(r):
                    continue
                phone = _phone_value(r)
                if not phone:
                    continue
                phone_str = str(phone).strip()
                if not PHONE_RE.match(phone_str):
                    continue
                if not _is_suspicious_placeholder_phone(phone_str):
                    continue
                ident = resource_identifier(rtype, r)
                rows_out.append([org, contract, rtype, str(r.get("id")), ident,
                                  all_identifiers(r), meta_source(r), resource_name(rtype, r),
                                  "A2010-DQ", "phone",
                                  "Phone number is a known placeholder/junk pattern",
                                  "A real, working 10-digit phone number",
                                  f"value={phone_str!r}", "FAIL"])
                n += 1
        if rows_out:
            w.writerow([f"Total Fail: {n}"])
        w.writerow(header)
        w.writerows(rows_out)
    return fname, n


def write_per_code_reports(org, contract, bundles, url_year=None):
    ctx = build_fhir_context(bundles)
    by_code = defaultdict(list)
    placeholder_excluded = defaultdict(int)
    for rtype, entries in bundles.items():
        for e in entries:
            r = e.get("resource", {})
            entry_url_year = url_year if rtype == "InsurancePlan" else None
            if is_placeholder_resource(r):
                for _field, _error, _expected, _actual, code in field_issues(rtype, r, ctx, entry_url_year, org):
                    placeholder_excluded[code] += 1
                broken_ref_code = PLACEHOLDER_TYPE_TO_CODE.get(rtype)
                if broken_ref_code:
                    by_code[broken_ref_code].append([
                        org, contract, rtype, str(r.get("id")), resource_identifier(rtype, r),
                        all_identifiers(r), meta_source(r), resource_name(rtype, r), "FAIL",
                        "(whole resource)", f"{rtype}/{r.get('id')} is referenced but was never actually included",
                        f"a real {rtype} resource supplied in the export",
                        "HAPI placeholder stub (see placeholders_{}.csv)".format(contract)])
                continue
            issues = field_issues(rtype, r, ctx, entry_url_year, org)
            failed_here = {}
            for field, error, expected, actual, code in issues:
                failed_here[code] = (field, error, expected, actual)
            org_codes = [c.get("code") for t in r.get("type", []) or [] for c in t.get("coding", []) or [] if c.get("code")]
            is_network_org = rtype == "Organization" and "ntwk" in org_codes
            loc_site_only = {"A2002", "A2005", "A2006", "A2007", "A2009", "A2010"}
            is_excluded_location = rtype == "Location" and _location_excluded_by_physical_type(r)
            for code, applicable_types in CODE_APPLICABLE_RESOURCE_TYPES.items():
                if rtype not in applicable_types:
                    continue
                if is_network_org and code != "A2010":
                    continue
                if is_excluded_location and code in loc_site_only:
                    continue
                ident = resource_identifier(rtype, r)
                base = [org, contract, rtype, str(r.get("id")), ident,
                        all_identifiers(r), meta_source(r), resource_name(rtype, r)]
                if code in failed_here:
                    field, error, expected, actual = failed_here[code]
                    by_code[code].append(base + ["FAIL", field, error, expected, actual])
                else:
                    by_code[code].append(base + ["PASS", "", f"Meets the {ERROR_CATALOG[code].name} requirement", "", ""])

    written = {}
    header = ["Organization", "Contract", "Resource Type", "Resource ID", "Identifier",
              "All Identifiers (system=value)", "Source (meta.source)", "Name", "Result",
              "Field", "Error", "Expected", "Actual"]
    for code, code_rows in by_code.items():
        edef = ERROR_CATALOG.get(code)
        name = edef.name if edef else code
        fname = f"code_{code}_{name}_{contract}.csv"
        fail_rows = [r for r in code_rows if r[8] == "FAIL"]
        pass_rows = [r for r in code_rows if r[8] == "PASS"]
        with open(fname, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if fail_rows:
                w.writerow([f"Total Fail: {len(fail_rows)}"])
            w.writerow(header)
            w.writerows(fail_rows)
            excluded = placeholder_excluded.get(code, 0)
            if excluded:
                w.writerow(["(note)", "", "", "", "", "", "", "", "",
                            "", f"{excluded} additional record(s) also failed this check but are "
                                f"placeholder stubs (referenced but never actually included in the "
                                f"export) -- not real, fixable records, so excluded here. See "
                                f"placeholders_{contract}.csv instead.", "", ""])
            if pass_rows:
                w.writerow([])
                w.writerows(pass_rows)
                w.writerow([f"Total Pass: {len(pass_rows)}"])
        written[code] = (fname, len(code_rows))
    return written


FILE_LEVEL_CODES = {"C4001", "C4002", "C4003", "C4004", "C4005", "C4006", "C4007", "C4008", "C4009",
                     "C4011", "C4012", "C4013", "C4014", "C4015", "C4016", "C4017", "C4018",
                     "N3015", "P1017"}


def write_file_level_code_reports(rows, contract):
    by_code = defaultdict(list)
    for r in rows[1:]:
        if r[12] != "FAIL":
            continue
        m = CODE_RE.search(r[11])
        if not m or m.group(1) not in FILE_LEVEL_CODES:
            continue
        code = m.group(1)
        by_code[code].append([r[0], r[1], r[2], r[3], r[12], r[13]])

    written = {}
    header = ["Organization", "Contract", "File Role", "URL", "Result", "Detail"]
    for code, code_rows in by_code.items():
        edef = ERROR_CATALOG.get(code)
        name = edef.name if edef else code
        fname = f"code_{code}_{name}_{contract}.csv"
        with open(fname, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([f"Total Fail: {len(code_rows)}"])
            w.writerow(header)
            w.writerows(code_rows)
        written[code] = (fname, len(code_rows))
    return written


def write_fatal_error_summary(rows, contract):
    fname = f"fatal_error_summary_{contract}.csv"
    header = ["Organization", "Contract", "Error Code", "Error Name", "File Role", "URL", "Result", "Detail"]
    fail_rows, pass_rows = [], []
    for r in rows[1:]:
        m = CODE_RE.search(r[11])
        if not m or m.group(1) not in FILE_LEVEL_CODES:
            continue
        code = m.group(1)
        edef = ERROR_CATALOG.get(code)
        name = edef.name if edef else code
        out = [r[0], r[1], code, name, r[2], r[3], r[12], r[13]]
        if r[12] == "FAIL":
            fail_rows.append(out)
        elif r[12] == "PASS":
            pass_rows.append(out)

    with open(fname, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if fail_rows:
            w.writerow([f"Total Fail: {len(fail_rows)}"])
            w.writerow(header)
            w.writerows(fail_rows)
        if fail_rows and pass_rows:
            w.writerow([])
        if pass_rows:
            if not fail_rows:
                w.writerow(header)
            w.writerows(pass_rows)
            w.writerow([f"Total Pass: {len(pass_rows)}"])
        if not fail_rows and not pass_rows:
            w.writerow(header)
    return fname, len(fail_rows), len(pass_rows)


def write_missing_fields_details(org, contract, bundles, url_year=None):
    fname = f"missing_required_fields_{contract}.csv"
    n = 0
    ctx = build_fhir_context(bundles)
    grouped = Counter()
    example = {}
    code_examples = {}
    with open(fname, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Organization", "Contract", "Error Code", "Error Name", "Level",
                    "Resource Type", "Resource ID", "Identifier", "All Identifiers (system=value)",
                    "Source (meta.source)", "Name", "Field", "Error", "Expected", "Actual"])
        for rtype, entries in bundles.items():
            for e in entries:
                r = e.get("resource", {})
                if is_placeholder_resource(r):
                    continue
                for field, error, expected, actual, code in field_issues(rtype, r, ctx, url_year, org):
                    ident = resource_identifier(rtype, r)
                    smile = meta_source(r)
                    edef = ERROR_CATALOG.get(code)
                    w.writerow([org, contract, code, edef.name if edef else "", edef.level if edef else "",
                                rtype, str(r.get("id")), ident,
                                all_identifiers(r), smile, resource_name(rtype, r),
                                field, error, expected, actual])
                    n += 1
                    key = (rtype, field, error, code)
                    grouped[key] += 1
                    if key not in example:
                        example[key] = ident or str(r.get("id"))
                    if code not in code_examples:
                        code_examples[code] = (rtype, ident or f"id:{r.get('id')}", smile, expected, actual)

    breakdown = [(rt, fld, err, code, cnt, example[(rt, fld, err, code)])
                 for (rt, fld, err, code), cnt in grouped.most_common()]
    return fname, n, breakdown, code_examples


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------
def process_plan(org, contract, index_url, rows, global_code_examples=None):
    if global_code_examples is None:
        global_code_examples = defaultdict(list)
    print(f"\n=== {org} {contract} ===")

    cert = check_tls_certificate(index_url)
    print("  TLS Certificate Check (C4002):")
    if cert["ok"] is None:
        print(f"    {cert['error']}")
    elif cert["ok"] is False:
        print(f"    FAILED: {cert['error']}")
        rows.append([org, contract, "index", index_url, "", "", "", "", "", "", "",
                     code_row_suffix("C4002"), "FAIL", cert["error"]])
    else:
        print(f"    Issued On:  {_fmt_cert_dt(cert['not_before'])}")
        print(f"    Expires On: {_fmt_cert_dt(cert['not_after'])}")
        print(f"    Issuer:     {cert['issuer']}")
        expiry_note = "EXPIRED" if cert["expired"] else f"valid, {cert['days_until_expiry']} day(s) remaining"
        print(f"    Status:     {expiry_note}")
        rows.append([org, contract, "index", index_url, "", "", "", "", "", "",
                     f"expires {cert['not_after'].date()}",
                     code_row_suffix("C4002"), "FAIL" if cert["expired"] else "PASS",
                     f"Certificate issued {cert['not_before'].date()}, expires {cert['not_after'].date()}"
                     f"{' -- EXPIRED' if cert['expired'] else ''} (issuer: {cert['issuer']})"])

    meta, body = check_http_metadata(index_url)
    rows.append([org, contract, "index", index_url, meta["status"], meta["head_supported"],
                 meta["conditional_get_304"], meta["content_type_ok"], meta["etag_present"],
                 meta["last_modified_present"], "", code_row_suffix("C4008") + " / HTTP metadata (Appendix D)",
                 "PASS" if meta["status"] == 200 and meta["head_supported"] and meta["conditional_get_304"] else "CHECK",
                 f"status={meta['status']}"])
    rows.append([org, contract, "index", index_url, meta["status"], "", "", "", "", "", "",
                 code_row_suffix("C4005"), "PASS" if meta["head_supported"] else "FAIL",
                 "HEAD request supported" if meta["head_supported"] else "HEAD request unsupported or failed"])
    rows.append([org, contract, "index", index_url, meta["status"], "", "", "", "", "", "",
                 code_row_suffix("C4006"), "PASS" if meta["last_modified_present"] else "FAIL",
                 "Last-Modified header present" if meta["last_modified_present"] else "Last-Modified header missing"])
    rows.append([org, contract, "index", index_url, meta["status"], "", "", "", "", "", "",
                 code_row_suffix("C4007"), "PASS" if meta.get("content_length_present") else "FAIL",
                 "Content-Length header present" if meta.get("content_length_present") else "Content-Length header missing"])
    rows.append([org, contract, "index", index_url, meta["status"], "", "", "", "", "", "",
                 code_row_suffix("C4009"), "PASS" if meta["etag_present"] else "FAIL",
                 "ETag header present" if meta["etag_present"] else "ETag header missing"])
    rows.append([org, contract, "index", index_url, meta["status"], "", "", "", "", "",
                 "", code_row_suffix("C4010"),
                 "FAIL" if meta.get("stale_over_30_days") else ("PASS" if meta.get("stale_over_30_days") is False else "INFO"),
                 "Last-Modified header age check"])

    if meta["status"] != 200:
        code = "C4001" if meta["status"] is None else "C4003"
        reason = f" -- {body.decode('utf-8', 'replace')[:200]}" if meta["status"] is None and body else ""
        rows.append([org, contract, "index", index_url, meta["status"], "", "", "", "", "", "",
                     code_row_suffix(code), "FAIL", f"Index file did not return 200 (got {meta['status']}){reason}"])
        print(f"  FAILED: index file returned status {meta['status']}  [{code}]{reason}")
        return

    challenge = detect_challenge_page(body, meta.get("content_type"))
    if challenge:
        rows.append([org, contract, "index", index_url, meta["status"], "", "", "", "", "", "",
                     code_row_suffix("C4004"), "FAIL", challenge])
        print(f"  FAILED: {challenge}  [C4004]")
        print(f"  -> Aborting this plan: the endpoint returned a challenge page, not data. "
              f"This is not a data-quality issue -- check IP allowlisting / User-Agent / TLS requirements with the host.")
        return

    try:
        index_doc = json.loads(body)
    except Exception as e:
        rows.append([org, contract, "index", index_url, meta["status"], "", "", "", "", "", "",
                     code_row_suffix("C4004"), "FAIL", f"Index file is not valid JSON: {e}"])
        print("  FAILED: index file is not valid JSON  [C4004]")
        return
    rows.append([org, contract, "index", index_url, meta["status"], "", "", "", "", "",
                 f"Bundle/IndexFile/ {len(body)} bytes",
                 code_row_suffix("C4004"), "PASS", "Index file parsed as valid JSON"])
    print(f"  [C4004] PASS - Index file parsed as valid JSON")

    provider_urls = index_doc.get("provider_urls", [])
    if not isinstance(index_doc, dict) or "provider_urls" not in index_doc:
        rows.append([org, contract, "index", index_url, meta["status"], "", "", "", "", "", "",
                     code_row_suffix("C4013"), "FAIL", "Index JSON has no top-level 'provider_urls' array"])
        print(f"  [C4013] FAIL - Index JSON has no top-level 'provider_urls' array")
    elif not provider_urls:
        rows.append([org, contract, "index", index_url, meta["status"], "", "", "", "", "", "",
                     code_row_suffix("C4012"), "FAIL", "'provider_urls' array is present but empty"])
        print(f"  [C4012] FAIL - 'provider_urls' array is present but empty")
    else:
        rows.append([org, contract, "index", index_url, meta["status"], meta["head_supported"],
                     meta["conditional_get_304"], meta["content_type_ok"], meta["etag_present"],
                     meta["last_modified_present"], f"{len(provider_urls)} provider_urls",
                     code_row_suffix("C4013"), "PASS", f"Index JSON has a top-level 'provider_urls' array ({len(provider_urls)} entries)"])
        print(f"  [C4013] PASS - Index JSON has a top-level 'provider_urls' array")
        rows.append([org, contract, "index", index_url, "", "", "", "", "", "", f"{len(provider_urls)} provider_urls",
                     code_row_suffix("C4012"), "PASS", f"'provider_urls' array is non-empty ({len(provider_urls)} URLs)"])
        print(f"  [C4012] PASS - 'provider_urls' array is non-empty ({len(provider_urls)} URLs)")
    print(f"  Index OK. {len(provider_urls)} constituent file(s).")

    def _is_blank_url(u):
        return u is None or (isinstance(u, str) and not u.strip())

    blank_urls = [u for u in provider_urls if _is_blank_url(u)]
    for _ in blank_urls:
        rows.append([org, contract, "index", index_url, "", "", "", "", "", "", "",
                     code_row_suffix("C4011"), "FAIL", "provider_urls entry is missing/blank"])
    if blank_urls:
        print(f"  [C4011] FAIL - {len(blank_urls)} missing/blank provider_urls entry(ies)")
    if provider_urls and not blank_urls:
        rows.append([org, contract, "index", index_url, "", "", "", "", "", "", f"{len(provider_urls)} URLs checked",
                     code_row_suffix("C4011"), "PASS", "No missing/blank provider_urls entries"])
        print(f"  [C4011] PASS - No missing/blank provider_urls entries")

    def _is_malformed_url(u):
        return (not isinstance(u, str) or not re.match(r"^https://\S+$", u)
                or " " in u or "," in u or "\n" in u)

    non_blank_urls = [u for u in provider_urls if not _is_blank_url(u)]
    malformed_urls = [u for u in non_blank_urls if _is_malformed_url(u)]
    for u in malformed_urls:
        rows.append([org, contract, "index", index_url, "", "", "", "", "", "", str(u)[:120],
                     code_row_suffix("C4014"), "FAIL", f"Malformed provider_urls entry: {u!r}"])
    if malformed_urls:
        print(f"  [C4014] FAIL - {len(malformed_urls)} malformed provider_urls entry(ies)")
    if non_blank_urls and not malformed_urls:
        rows.append([org, contract, "index", index_url, "", "", "", "", "", "", f"{len(non_blank_urls)} URLs checked",
                     code_row_suffix("C4014"), "PASS", "All provider_urls entries are well-formed https:// URLs"])
        print(f"  [C4014] PASS - All {len(non_blank_urls)} provider_urls entries are well-formed https:// URLs")

    bundles = defaultdict(list)
    mr_providers = []
    file_summary = defaultdict(lambda: {"files": [], "indexed": 0, "skipped": False})

    def _file_category(furl_):
        m = FILE_CATEGORY_RE.search(furl_)
        return m.group(2).lower() if m else furl_.rsplit("/", 1)[-1]

    SKIP_TYPES = ("healthcareservice",)

    for furl in provider_urls:
        role = furl.rsplit("/", 1)[-1]
        cat = _file_category(furl)
        if any(k in role.lower() for k in SKIP_TYPES):
            print(f"  skipping {role} (resource type not consumed by MPF)")
            rows.append([org, contract, role, furl, "", "", "", "", "", "", "",
                         "skipped (not MPF-consumed)", "INFO",
                         "Resource type is not one of the 7 MPF-consumed types; skipped for speed"])
            file_summary[cat]["files"].append(role)
            file_summary[cat]["skipped"] = True
            continue
        print(f"  fetching {role} ...", end=" ", flush=True)
        t0 = time.time()
        fmeta, fbody = check_http_metadata(furl)
        elapsed = time.time() - t0
        if fmeta["status"] is None:
            print(f"status=None ({elapsed:.1f}s) ERROR: {fbody.decode('utf-8', 'replace')[:200]}")
        else:
            print(f"status={fmeta['status']} ({elapsed:.1f}s, {len(fbody)/1024:.0f} KB)")

        rows.append([org, contract, role, furl, fmeta["status"], fmeta["head_supported"],
                     fmeta["conditional_get_304"], fmeta["content_type_ok"], fmeta["etag_present"],
                     fmeta["last_modified_present"], "", "HTTP metadata (Appendix D)",
                     "PASS" if fmeta["status"] == 200 else "FAIL", f"status={fmeta['status']}"])
        rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "",
                     "", code_row_suffix("C4010"),
                     "FAIL" if fmeta.get("stale_over_30_days") else ("PASS" if fmeta.get("stale_over_30_days") is False else "INFO"),
                     "Last-Modified header age check"])

        if fmeta["status"] != 200:
            code = "C4001" if fmeta["status"] is None else "C4003"
            reason = f" -- {fbody.decode('utf-8', 'replace')[:200]}" if fmeta["status"] is None and fbody else ""
            rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", "",
                         code_row_suffix(code), "FAIL", f"Constituent file did not return 200 (got {fmeta['status']}){reason}"])
            print(f"    [{code}] FAIL - {role} did not return 200 (got {fmeta['status']}){reason}")
            continue
        rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", "",
                     code_row_suffix("C4001"), "PASS", f"{role} reachable (status={fmeta['status']})"])
        rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", "",
                     code_row_suffix("C4003"), "PASS", f"{role} retrieved successfully (200 OK)"])
        print(f"    [C4001] PASS - {role} reachable  |  [C4003] PASS - {role} retrieved (200 OK)")

        challenge = detect_challenge_page(fbody, fmeta.get("content_type"))
        if challenge:
            rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", "",
                         code_row_suffix("N3015"), "FAIL", challenge])
            print(f"    [N3015] FAIL - {challenge}")
            continue

        try:
            doc = json.loads(fbody)
        except Exception as e:
            rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", "",
                         code_row_suffix("N3015"), "FAIL", f"Not valid JSON: {e}"])
            print(f"    [N3015] FAIL - {role} is not valid JSON: {e}")
            continue

        if isinstance(doc, dict) and doc.get("resourceType") == "Bundle":
            entries = doc.get("entry", []) or []
            if not entries:
                rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", "",
                             code_row_suffix("C4018"), "FAIL", "FHIR Bundle is valid JSON but contains zero entries"])
                print(f"    [C4018] FAIL - {role}: Bundle is valid JSON but has zero entries")
            else:
                rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", f"{len(entries)} entries",
                             code_row_suffix("C4018"), "PASS", f"FHIR Bundle has {len(entries)} entries"])
                print(f"    [C4018] PASS - {role}: Bundle has {len(entries)} entries")
            rtypes = Counter(e.get("resource", {}).get("resourceType") for e in entries)
            rtype = rtypes.most_common(1)[0][0] if rtypes else "Unknown"
            rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", f"Bundle/{rtype}/ {len(fbody)} bytes",
                         code_row_suffix("N3015"), "PASS", f"File parsed as valid JSON (Bundle of {rtype}, {len(entries)} entries)"])
            print(f"    [N3015] PASS - {role} parsed as valid JSON (Bundle of {rtype}, {len(entries)} entries)")
            rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", "",
                         code_row_suffix("C4015"), "PASS", f"File conforms to a recognized plan-data shape (FHIR Bundle of {rtype})"])
            bundles[rtype].extend(entries)
            file_summary[cat]["files"].append(role)
            file_summary[cat]["indexed"] += len(entries)
        elif isinstance(doc, dict) and doc.get("resourceType") and doc.get("resourceType") != "Bundle":
            rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", "",
                         code_row_suffix("C4017"), "INFO",
                         f"File is a single {doc.get('resourceType')} resource, not wrapped in a Bundle"])
            print(f"    [C4017] INFO - {role}: single {doc.get('resourceType')} resource, not wrapped in a Bundle")
            rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "",
                         f"Bundle/{doc['resourceType']}/ {len(fbody)} bytes",
                         code_row_suffix("N3015"), "PASS", f"File parsed as valid JSON (single {doc['resourceType']} resource)"])
            print(f"    [N3015] PASS - {role} parsed as valid JSON (single {doc['resourceType']} resource)")
            rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", "",
                         code_row_suffix("C4015"), "PASS", f"File conforms to a recognized plan-data shape (single {doc['resourceType']} resource)"])
            bundles[doc["resourceType"]].append({"resource": doc})
            file_summary[cat]["files"].append(role)
            file_summary[cat]["indexed"] += 1
        elif isinstance(doc, list):
            rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "",
                         f"array/MachineReadableProvider {len(fbody)} bytes",
                         code_row_suffix("N3015"), "PASS", f"File parsed as valid JSON (machine-readable array, {len(doc)} records)"])
            print(f"    [N3015] PASS - {role} parsed as valid JSON (machine-readable array, {len(doc)} records)")
            malformed_entries = Bucket()
            for i, entry in enumerate(doc):
                if not isinstance(entry, dict):
                    malformed_entries.hit(f"index {i}: entry is {type(entry).__name__}, not an object")
                elif not (entry.get("npi") or entry.get("facilityName")) or not entry.get("type"):
                    malformed_entries.hit(f"index {i}: missing npi/facilityName and/or type field")
            if malformed_entries.count:
                rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", "",
                             code_row_suffix("C4016"), "FAIL",
                             f"{malformed_entries.count} of {len(doc)} entries do not conform to the machine-readable "
                             f"provider schema{malformed_entries.suffix()}"])
                print(f"    [C4016] FAIL - {role}: {malformed_entries.count} of {len(doc)} entries do not conform to schema")
            else:
                rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", f"{len(doc)} records",
                             code_row_suffix("C4016"), "PASS",
                             f"All {len(doc)} entries conform to the machine-readable provider schema"])
                print(f"    [C4016] PASS - {role}: all {len(doc)} entries conform to schema")
            rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "", "",
                         code_row_suffix("C4015"), "PASS", "File conforms to a recognized plan-data shape (machine-readable array)"])
            mr_providers.extend(doc)
            file_summary[cat]["files"].append(role)
            file_summary[cat]["indexed"] += len(doc)
        else:
            rows.append([org, contract, role, furl, fmeta["status"], "", "", "", "", "",
                         "unknown", code_row_suffix("C4015"), "FAIL",
                         "File is neither a FHIR Bundle, a single FHIR resource, nor a machine-readable provider array"])
            print(f"    [C4015] FAIL - {role}: not a FHIR Bundle, single FHIR resource, or machine-readable array")
            file_summary[cat]["files"].append(role)

    write_resource_file_summary(org, contract, file_summary)

    total_provider_like = len(mr_providers) + len(bundles.get("PractitionerRole", [])) + \
        len(bundles.get("OrganizationAffiliation", []))
    rows.append([org, contract, "(cross-file)", "", "", "", "", "", "", "",
                 f"{total_provider_like} provider-like record(s) found", code_row_suffix("P1017"),
                 "FAIL" if total_provider_like == 0 else "PASS",
                 "No PractitionerRole/OrganizationAffiliation/machine-readable provider records found in any constituent file"
                 if total_provider_like == 0 else f"{total_provider_like} provider-like record(s) present"])
    if total_provider_like == 0:
        print(f"  [P1017] FAIL - No provider-like records found in any constituent file")
    else:
        print(f"  [P1017] PASS - {total_provider_like} provider-like record(s) found across all constituent files")

    npi_to_type = {}
    for e in bundles.get("Practitioner", []):
        npi = get_identifier(e.get("resource", {}), NPI_SYS)
        if npi and NPI_RE.match(str(npi)):
            npi_to_type[npi] = "Individual"
    for e in bundles.get("Organization", []):
        r = e.get("resource", {})
        codes = [c.get("code") for t in r.get("type", []) or [] for c in t.get("coding", []) or []]
        if "fac" in codes:
            npi = get_identifier(r, NPI_SYS)
            if npi and NPI_RE.match(str(npi)):
                npi_to_type[npi] = "Facility"
    for p in mr_providers:
        npi = p.get("npi")
        ptype = p.get("type")
        if npi and NPI_RE.match(str(npi)) and ptype in ("Individual", "Facility"):
            npi_to_type[npi] = ptype

    if npi_to_type and SKIP_NPPES_LOOKUP:
        print(f"  skipping NPPES registry lookup (SKIP_NPPES_LOOKUP=True) -- "
              f"{len(npi_to_type)} unique NPIs not checked against NPPES this run")
        for code in ("P1002", "P1003", "P1005"):
            rows.append([org, contract, "(cross-file: NPPES)", "", "", "", "", "", "", "",
                         "0 checked (skipped)", code_row_suffix(code), "INFO",
                         f"NPPES lookup skipped this run (SKIP_NPPES_LOOKUP=True); "
                         f"{len(npi_to_type)} unique NPIs not checked"])
    elif npi_to_type:
        print(f"  checking up to {min(len(npi_to_type), NPI_LOOKUP_MAX)} of {len(npi_to_type)} unique NPIs against NPPES ...",
              end=" ", flush=True)
        t0 = time.time()
        npi_rows, checked, total_unique = check_npi_registry(npi_to_type)
        print(f"done ({time.time() - t0:.1f}s, {checked} checked)")
        not_tested_npis = [(npi, f"NPI:{npi}") for npi in list(npi_to_type.keys())[NPI_LOOKUP_MAX:]]
        nppes_coverage = {"total": total_unique, "tested": checked, "not_tested_records": not_tested_npis}
        for check_name, result, detail in npi_rows:
            rows.append([org, contract, "(cross-file: NPPES)", "", "", "", "", "", "", "",
                         f"{checked} of {total_unique} unique NPIs checked", check_name, result, detail])
            print_coverage_block("NPI Registry (NPPES)", check_name, nppes_coverage)

    fhir_ctx = build_fhir_context(bundles)
    for rtype, entries in bundles.items():
        rtype_checks, rtype_coverage = validate_fhir_bundle(rtype, entries, fhir_ctx, org=org)
        for check_name, result, detail in rtype_checks:
            rows.append([org, contract, f"{rtype} (merged, {len(entries)} entries)", "", "", "", "", "", "", "",
                         f"Bundle/{rtype}/{len(entries)} entries", check_name, result, detail])
            print_coverage_block(rtype, check_name, rtype_coverage)

    if mr_providers:
        mr_checks, mr_coverage = validate_machine_readable(mr_providers)
        for check_name, result, detail in mr_checks:
            rows.append([org, contract, f"machine-readable (merged, {len(mr_providers)} providers)", "", "", "", "", "", "", "",
                         f"array/{len(mr_providers)} providers", check_name, result, detail])
            print_coverage_block("MachineReadableProvider", check_name, mr_coverage)

    for role, resource_info, check_name, result, detail in cross_checks(org, contract, index_url, index_doc, bundles, mr_providers):
        rows.append([org, contract, role, "", "", "", "", "", "", "", resource_info, check_name, result, detail])

    if bundles:
        for role, resource_info, check_name, result, detail in placeholder_checks(bundles):
            rows.append([org, contract, role, "", "", "", "", "", "", "", resource_info, check_name, result, detail])

        ph_file, ph_n, ph_code_ex = write_placeholder_details(contract, bundles)
        mf_file, mf_n, mf_breakdown, mf_code_ex = write_missing_fields_details(org, contract, bundles, extract_year_from_url(index_url))
        phone_file, phone_n, phone_code_ex, missing_phone_file, missing_phone_n = write_phone_details(org, contract, bundles)
        phone_dq_file, phone_dq_n = write_phone_data_quality_report(org, contract, bundles)
        per_code_files = write_per_code_reports(org, contract, bundles, extract_year_from_url(index_url))
        for code, (rtype, ident, smile, expected, actual) in {**ph_code_ex, **mf_code_ex, **phone_code_ex}.items():
            if len(global_code_examples[code]) < 10:
                global_code_examples[code].append((org, contract, rtype, ident, smile, expected, actual))
        rows.append([org, contract, "(cross-file)", "", "", "", "", "", "", "",
                     f"{ph_n} placeholder resource(s)", "placeholder details (missing/dangling resources)",
                     "INFO", f"per-ID list written to {ph_file}"])
        rows.append([org, contract, "(cross-file)", "", "", "", "", "", "", "",
                     f"{mf_n} field issue(s) on present resources", "missing required fields (field/error/expected/actual, excludes placeholders)",
                     "INFO", f"per-issue list (with identifier) written to {mf_file}"])
        rows.append([org, contract, "(cross-file)", "", "", "", "", "", "", "",
                     f"{phone_n} phone number issue(s) (missing [A2009] + invalid format [A2010])",
                     "MissingProviderPhoneNumber / InvalidProviderPhoneNumber [A2009/A2010] -- full per-record list",
                     "INFO", f"every failing record (Resource ID, NPI, Smile ID, Name) written to {phone_file}"])
        rows.append([org, contract, "(cross-file)", "", "", "", "", "", "", "",
                     f"{missing_phone_n} missing phone number(s) [A2009 only]",
                     "MissingProviderPhoneNumber [A2009] -- dedicated per-record list, all resource types",
                     "INFO", f"every A2009 record (Resource ID, NPI, Smile ID, Name) written to {missing_phone_file}"])
        rows.append([org, contract, "(cross-file)", "", "", "", "", "", "", "",
                     f"{phone_dq_n} format-valid but suspicious/placeholder phone number(s) [A2010-DQ, not an Appendix E code]",
                     "PhoneNumberDataQualityIssue [A2010-DQ] -- data-quality flag, separate from official A2010 report",
                     "INFO", f"every flagged record written to {phone_dq_file}"])

        for rtype, field, error, code, cnt, example_id in mf_breakdown:
            rtype_total = len(bundles.get(rtype, []))
            rows.append([org, contract, rtype, "", "", "", "", "", "", "",
                         f"Bundle/{rtype}/{rtype_total} entries",
                         f"field issue: {rtype}.{field} [{code}] {ERROR_CATALOG[code].name}", "INFO",
                         f"{cnt} of {rtype_total} {rtype} entries: {error} (example: {example_id})"])

        print(f"  -> {ph_n} placeholder (missing) resources: {ph_file}")
        print(f"  -> {mf_n} field issue(s) on present resources: {mf_file}")
        print(f"  -> {phone_n} phone number issue(s) (full per-record list): {phone_file}")
        print(f"  -> {missing_phone_n} missing phone number(s) [A2009 only, all resource types]: {missing_phone_file}")
        if per_code_files:
            print(f"  -> {len(per_code_files)} per-code report(s) written (one CSV per Appendix E code, every resource type combined):")
            for code, (fname, cnt) in sorted(per_code_files.items()):
                print(f"       [{code}] {cnt} record(s): {fname}")
            rows.append([org, contract, "(cross-file)", "", "", "", "", "", "", "",
                         f"{len(per_code_files)} per-code report(s)",
                         "Per-code failing-record reports -- one CSV per Appendix E code, all resource types combined",
                         "INFO", "; ".join(f"[{c}] {fn}" for c, (fn, _) in sorted(per_code_files.items()))])
        if mf_breakdown:
            print(f"  -> field issues by type ({len(mf_breakdown)} distinct):")
            for rtype, field, error, code, cnt, example_id in mf_breakdown[:10]:
                print(f"       {cnt:>6}  [{code}] {rtype}.{field}: {error} (e.g. {example_id})")
            if len(mf_breakdown) > 10:
                remaining = sum(c for _, _, _, _, c, _ in mf_breakdown[10:])
                print(f"       {remaining:>6}  ... {len(mf_breakdown) - 10} more issue type(s)")

    file_level_files = write_file_level_code_reports(rows, contract)
    if file_level_files:
        print(f"  -> {len(file_level_files)} file/HTTP-level per-code report(s) written:")
        for code, (fname, cnt) in sorted(file_level_files.items()):
            print(f"       [{code}] {cnt} record(s): {fname}")
        rows.append([org, contract, "(cross-file)", "", "", "", "", "", "", "",
                     f"{len(file_level_files)} file-level per-code report(s)",
                     "Per-code file/HTTP-level reports -- one CSV per Appendix E code",
                     "INFO", "; ".join(f"[{c}] {fn}" for c, (fn, _) in sorted(file_level_files.items()))])

    fatal_file, fatal_fail_n, fatal_pass_n = write_fatal_error_summary(rows, contract)
    print(f"  -> Fatal error summary: {fatal_fail_n} fail / {fatal_pass_n} pass -> {fatal_file}")
    rows.append([org, contract, "(cross-file)", "", "", "", "", "", "", "",
                 f"{fatal_fail_n} fatal (Level 1) failure(s)",
                 "Fatal Error Summary -- all Level 1 (C4xxx/N3015/P1017) results combined in one CSV",
                 "INFO", f"written to {fatal_file}"])

    return bundles, mr_providers


def main():
    plans = PLANS
    out_path = "ma_directory_validation_report.csv"

    if len(sys.argv) == 4:
        org, contract, url = sys.argv[1], sys.argv[2], sys.argv[3]
        plans = [(org, contract, url)]
        out_path = f"ma_directory_validation_report_{contract}.csv"
    elif len(sys.argv) == 2:
        plans = []
        with open(sys.argv[1], newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if not row or not row[0].strip():
                    continue
                org, contract, url = [c.strip() for c in row[:3]]
                plans.append((org, contract, url))

    rows = [["Organization", "Contract", "File Role", "URL", "HTTP Status", "HEAD Supported",
             "Conditional GET (304)", "Content-Type OK", "ETag Present", "Last-Modified Present",
             "Resource Info", "Check", "Result", "Detail"]]

    global_code_examples = defaultdict(list)
    for org, contract, url in plans:
        process_plan(org, contract, url, rows, global_code_examples)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)

    fails = [r for r in rows[1:] if r[12] == "FAIL"]
    passes = [r for r in rows[1:] if r[12] == "PASS"]
    print("\n" + "=" * 60)
    print(f"SUMMARY: {len(passes)} checks passed, {len(fails)} checks failed")
    print(f"Report written to: {out_path}")
    if fails:
        print("\nFAILING TEST CASES (expected vs actual, with Appendix E error code):")
        for r in fails:
            m = CODE_RE.search(r[11])
            code = m.group(1) if m else "(untagged)"
            level = ERROR_CATALOG[code].level if code in ERROR_CATALOG else "?"
            label = "WARN" if code in WARNING_ONLY_CODES else "FAIL"
            print(f"  [{r[0]} {r[1]}] {r[2]}")
            print(f"      check    : {r[11]}")
            print(f"      code     : {code} (Level {level})")
            print(f"      expected : {expected_for(code) if code != '(untagged)' else '(no Appendix E code tagged)'}")
            print(f"      actual   : {label} -- {r[13]}")

    cov_path = write_appendix_e_coverage(rows, out_path, global_code_examples)
    label = out_path.replace("ma_directory_validation_report", "").replace(".csv", "").strip("_") or "ALL"
    ab_file = write_appendix_b_summary(cov_path, label)
    if ab_file:
        print(f"Appendix B (FHIR-Based JSON Specifications) summary written to: {ab_file}")
    return rows, out_path


CODE_RE = re.compile(r"\[([A-Z]\d{4})\]")
TOTAL_TESTED_RE = re.compile(r"^\s*[\d,]+\s+of\s+([\d,]+)\b")
FAIL_RECORD_COUNT_RE = re.compile(r"^\s*([\d,]+)\s+of\s+[\d,]+\b")
NPPES_SAMPLE_RE = re.compile(r"\[sampled\s+[\d,]+\s+of\s+([\d,]+)\s+unique NPIs")
RESOURCE_TYPE_FROM_INFO_RE = re.compile(r"Bundle/([A-Za-z]+)/")

CODE_TO_COMPANION_PREFIXES = {}
for _code in ("P1001", "P1004", "P1006", "P1007", "P1008", "P1009", "P1010", "P1011",
              "P1016", "N3001", "N3002", "N3004", "N3005", "F5001", "F5009", "C4010",
              "A2001", "A2002", "A2003", "A2004", "A2005", "A2006", "A2007"):
    CODE_TO_COMPANION_PREFIXES.setdefault(_code, set()).add("missing_required_fields")
for _code in ("A2009", "A2010"):
    CODE_TO_COMPANION_PREFIXES.setdefault(_code, set()).add("phone_number_issues")
for _code in ("F5005", "F5006", "F5007", "F5008"):
    CODE_TO_COMPANION_PREFIXES.setdefault(_code, set()).add("placeholders")


def write_appendix_e_coverage(rows, out_path, code_examples=None):
    code_examples = code_examples or {}
    seen = defaultdict(lambda: {"PASS": 0, "FAIL": 0})
    total_tested = defaultdict(int)
    not_tested = defaultdict(int)
    fail_records = defaultdict(int)
    code_resource_types = defaultdict(list)
    for r in rows[1:]:
        m = CODE_RE.search(r[11])
        if m and r[12] in ("PASS", "FAIL"):
            code = m.group(1)
            seen[code][r[12]] += 1
            tm = TOTAL_TESTED_RE.match(r[13])
            row_tested = int(tm.group(1).replace(",", "")) if tm else 0
            if tm:
                total_tested[code] += row_tested
            fm = FAIL_RECORD_COUNT_RE.match(r[13])
            if fm:
                fail_records[code] += int(fm.group(1).replace(",", ""))
            sm = NPPES_SAMPLE_RE.search(r[13])
            if sm:
                total_unique = int(sm.group(1).replace(",", ""))
                not_tested[code] += max(0, total_unique - row_tested)
            rt_matches = RESOURCE_TYPE_FROM_INFO_RE.findall(r[10])
            if rt_matches:
                rtype_labels = rt_matches
            elif r[2] == "(cross-file: NPPES)":
                rtype_labels = ["NPI Registry (NPPES)"]
            elif str(r[10]).startswith("array/"):
                rtype_labels = ["MachineReadableProvider"]
            else:
                rtype_labels = []
            for rtype_label in rtype_labels:
                if rtype_label not in code_resource_types[code]:
                    code_resource_types[code].append(rtype_label)

    cov_path = out_path.replace(".csv", "") + "_appendix_e_coverage.csv"
    contracts_seen = sorted({(r[0], r[1]) for r in rows[1:] if r[0] and r[1]})
    with open(cov_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["error_code", "level", "bug_title", "status", "pass_count", "fail_count",
                     "total_records_tested", "total_records_not_tested", "not_tested_reason", "failing_record_count",
                     "org", "contract", "resource_type", "identifier", "smile_id",
                     "expected", "actual", "companion_csv_file(s)", "note"])
        for code, edef in sorted(ERROR_CATALOG.items()):
            prefixes = CODE_TO_COMPANION_PREFIXES.get(code, set())
            examples = code_examples.get(code, [])
            tested = total_tested.get(code, 0)
            skipped = not_tested.get(code, 0)
            failrecs = fail_records.get(code, 0)
            skip_reason = (f"NPPES registry is rate-limited to {NPI_LOOKUP_MAX} unique NPIs per contract "
                            f"-- the remaining {skipped:,} unique NPIs were not checked against NPPES") if skipped else ""
            if code in seen:
                s = seen[code]
                status = "FAIL_SEEN" if s["FAIL"] else "PASS_ONLY"
                if status == "FAIL_SEEN" and examples:
                    for org, contract, rtype, ident, smile, expected, actual in examples:
                        cf_str = ", ".join(sorted(f"{p}_{contract}.csv" for p in prefixes))
                        w.writerow([code, edef.level, edef.name, status, s["PASS"], s["FAIL"], tested, skipped, skip_reason, failrecs,
                                    org, contract, rtype, ident, smile or "(no Smile ID)",
                                    expected, actual, cf_str, ""])
                elif status == "FAIL_SEEN":
                    cf_str = ", ".join(sorted(f"{p}_{c}.csv" for p in prefixes for _, c in contracts_seen))
                    rtypes_str = ", ".join(code_resource_types.get(code, []))
                    w.writerow([code, edef.level, edef.name, status, s["PASS"], s["FAIL"], tested, skipped, skip_reason, failrecs,
                                "", "", rtypes_str, "", "", "", "", cf_str,
                                "FAIL seen but no single-record example captured -- see the main report's Detail column for this check"])
                else:
                    rtypes_str = ", ".join(code_resource_types.get(code, []))
                    w.writerow([code, edef.level, edef.name, status, s["PASS"], s["FAIL"], tested, skipped, skip_reason, failrecs,
                                "", "", rtypes_str, "", "", "", "", "", ""])
            elif code in NOT_IMPLEMENTED_CODES:
                ni = NOT_IMPLEMENTED_CODES[code]
                note = (f"REASON: {ni['reason']} | WHAT IT WOULD CHECK: {ni['what_it_would_check']} | "
                        f"REQUIRES: {ni['requires']} | RISK IF SKIPPED: {ni['risk_if_skipped']}")
                w.writerow([code, edef.level, edef.name, "NOT_IMPLEMENTED", 0, 0, 0, 0, "", 0,
                            "", "", "", "", "", "", "", "", note])
            else:
                w.writerow([code, edef.level, edef.name, "NOT_TRIGGERED", 0, 0, 0, 0, "", 0,
                            "", "", "", "", "", "", "", "", "No data in this run exercised this check"])

    status_of = {}
    for code, edef in sorted(ERROR_CATALOG.items()):
        if code in seen:
            status_of[code] = "FAIL_SEEN" if seen[code]["FAIL"] else "PASS_ONLY"
        elif code in NOT_IMPLEMENTED_CODES:
            status_of[code] = "NOT_IMPLEMENTED"
        else:
            status_of[code] = "NOT_TRIGGERED"

    n_fail_seen = sum(1 for s in status_of.values() if s == "FAIL_SEEN")
    n_pass_only = sum(1 for s in status_of.values() if s == "PASS_ONLY")
    n_not_impl = sum(1 for s in status_of.values() if s == "NOT_IMPLEMENTED")
    n_not_trig = sum(1 for s in status_of.values() if s == "NOT_TRIGGERED")

    print("\n" + "=" * 88)
    print("APPENDIX E COVERAGE -- FULL DETAIL (grouped by Level, per the Technical Implementation Guide, page 30)")
    print("=" * 88)
    print(f"Totals: {n_fail_seen} FAIL_SEEN | {n_pass_only} PASS_ONLY | {n_not_trig} NOT_TRIGGERED | {n_not_impl} NOT_IMPLEMENTED")

    STATUS_LABEL = {
        "FAIL_SEEN": "FAIL",
        "PASS_ONLY": "PASS",
        "NOT_TRIGGERED": "N/A ",
        "NOT_IMPLEMENTED": "N/A ",
    }

    for level in (1, 2, 3):
        level_name, level_desc = LEVEL_DESCRIPTIONS[level]
        print(f"\n{'-' * 88}")
        print(f"Level {level} - {level_name}")
        print(f"{'-' * 88}")
        for line in _wrap(level_desc, 88):
            print(line)
        print()
        codes_at_level = [c for c, e in sorted(ERROR_CATALOG.items()) if e.level == level]
        col = ["Status", "Code", "Name", "Checks", "Total", "Tested", "NotTested", "FailRecs", "Coverage", "ResourceType(s)"]
        widths = [6, 6, 33, 8, 9, 9, 9, 9, 9, 65]
        header = "  " + "  ".join(h.ljust(w) for h, w in zip(col, widths))
        print(header)
        print("  " + "-" * (len(header) - 2))
        for code in codes_at_level:
            edef = ERROR_CATALOG[code]
            status = status_of[code]
            label = "WARN" if status == "FAIL_SEEN" and code in WARNING_ONLY_CODES else STATUS_LABEL[status]
            s = seen.get(code, {"PASS": 0, "FAIL": 0})
            tested = total_tested.get(code, 0)
            skipped = not_tested.get(code, 0)
            failrecs = fail_records.get(code, 0)
            total = tested + skipped
            if status == "FAIL_SEEN":
                fail_letter = "W" if code in WARNING_ONLY_CODES else "F"
                checks_str = f"{s['FAIL']}{fail_letter}/{s['PASS']}P"
            elif status == "PASS_ONLY":
                checks_str = f"{s['PASS']}P"
            else:
                checks_str = "-"
            if status in ("FAIL_SEEN", "PASS_ONLY") and total:
                pct_str = "100.00%" if skipped == 0 else f"{100.0 * tested / total:.2f}%"
                total_str, tested_str, skipped_str = f"{total:,}", f"{tested:,}", f"{skipped:,}"
                failrecs_str = f"{failrecs:,}"
            else:
                total_str = tested_str = skipped_str = failrecs_str = pct_str = "-"
            rtypes = code_resource_types.get(code, [])
            rtypes_joined = ", ".join(rtypes) if rtypes else "-"
            rtypes_col = (rtypes_joined[:62] + "...") if len(rtypes_joined) > 65 else rtypes_joined
            row_vals = [label, code, edef.name, checks_str, total_str, tested_str, skipped_str, failrecs_str, pct_str, rtypes_col]
            print("  " + "  ".join(str(v).ljust(w) for v, w in zip(row_vals, widths)))
            if rtypes and rtypes_joined != rtypes_col:
                print(f"    Full Resource Type List: {rtypes_joined}")
            if status == "NOT_IMPLEMENTED":
                ni = NOT_IMPLEMENTED_CODES[code]
                print(f"    -> not run: {ni['reason']}")
                print(f"       What it would check: {ni['what_it_would_check']}")
                print(f"       Requires:            {ni['requires']}")
                print(f"       Risk if skipped:     {ni['risk_if_skipped']}")
            elif status == "NOT_TRIGGERED":
                print(f"    -> not run: no data in this submission exercised this check")

    print(f"\nFull CSV breakdown written to: {cov_path}")
    return cov_path


APPENDIX_B_RESOURCE_TYPES = {"Practitioner", "PractitionerRole", "Location",
                             "Organization", "OrganizationAffiliation", "InsurancePlan"}
APPENDIX_B_EXTRA_CODES = {"F5005", "F5006", "F5007", "F5008"}


def write_appendix_b_summary(cov_path, contract):
    if not os.path.exists(cov_path):
        return None
    fname = f"appendix_b_summary_{contract}.csv"
    with open(cov_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows_out = []
        for row in reader:
            code = row.get("error_code", "")
            rtypes = row.get("resource_type", "") or ""
            is_appendix_b = (code in APPENDIX_B_EXTRA_CODES or
                             any(t in rtypes for t in APPENDIX_B_RESOURCE_TYPES))
            if is_appendix_b:
                rows_out.append(row)
    with open(fname, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows_out)
    return fname


def _wrap(text, width):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


if __name__ == "__main__":
    main()
