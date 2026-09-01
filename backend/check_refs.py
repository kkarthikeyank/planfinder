"""
Find dangling FHIR references (e.g. OrganizationAffiliation -> Location/4136336 that doesn't exist).

Usage:
    python check_refs.py H1619        # one contract
    python check_refs.py              # all contracts

Every run downloads fresh from the live endpoint -- nothing is written to disk
and nothing is cached between runs. Each constituent file is streamed into
memory, parsed exactly ONCE, and immediately reduced to two lightweight
things before the raw bytes/parsed JSON are dropped:
  - every (resourceType, id) that actually exists (for connectivity checks)
  - every (source type/id, field, target type/id) reference found in it
Only after every file has been read this way does it check each collected
reference against the collected existing-id set -- so no file ever needs to
be read a second time, and only the extracted tuples (not the full parsed
resources) are held in memory for the duration of the run.

Outputs (per contract) -- CSVs only, nothing else is written to disk:
  dangling_refs_<C>.csv      one row per broken reference
  dangling_summary_<C>.csv   affected counts + % per source type -> target type
"""
import json, csv, re, sys, urllib.request, urllib.error, time

CONTRACTS = {
    "H1619": "https://medicare-advantage-plan-finder-provider-directory.jeffersonhealthplans.com/h1619/2027/index.json",
    "H3124": "https://medicare-advantage-plan-finder-provider-directory.jeffersonhealthplans.com/h3124/2027/index.json",
    "H9207": "https://medicare-advantage-plan-finder-provider-directory.jeffersonhealthplans.com/h9207/2027/index.json",
    "H5826": "https://medicare-advantage-plan-finder-provider-directory.interop.chpw.org/h5826/2027/index.json",
}

# Prefix-agnostic: matches jhp-H1619-2027-location-part1.json AND any other org's
# <prefix>-<contract>-<year>-<category>-part<n>.json layout (e.g. CHPW's H5826 files).
FNAME_RE = re.compile(r"(?P<contract>[hH]\d+)-(?P<year>\d+)-(?P<category>[a-z]+)-part(?P<part>\d+)\.json", re.I)
# "Location/4136336", "Location/4136336/_history/1", or an absolute URL ending in Type/id
REF_RE = re.compile(r"(?:^|/)(?P<type>[A-Z][A-Za-z]+)/(?P<id>[A-Za-z0-9\-.]{1,64})(?:/_history/.*)?$")

PLACEHOLDER_EXT_URL = "http://hapifhir.io/fhir/StructureDefinition/resource-placeholder"


def has_placeholder_extension(resource):
    """True if the resource itself is explicitly marked placeholder data via the
    official 'resource-placeholder' extension (valueBoolean=true) -- the authoritative
    signal, as opposed to guessing from the shape of the id (see is_placeholder_id)."""
    for ext in resource.get("extension", []) or []:
        if ext.get("url") == PLACEHOLDER_EXT_URL and ext.get("valueBoolean") is True:
            return True
    return False

PLACEHOLDER_LITERALS = {
    "test", "example", "unknown", "tbd", "n/a", "na", "none", "null",
    "sample", "dummy", "fake", "todo", "xxx", "0000000000",
}


def is_placeholder_id(rid):
    """True if a target id looks like dummy/test data rather than a real FHIR id
    (all-zero, all-same-digit, simple sequential digits, or a known dummy literal),
    even though it may still happen to match a real resource in `existing`."""
    s = str(rid).strip()
    low = s.lower()
    if low in PLACEHOLDER_LITERALS:
        return True
    if s.isdigit():
        if len(set(s)) == 1:          # e.g. "0000", "1111"
            return True
        asc = "".join(str((int(s[0]) + i) % 10) for i in range(len(s)))
        desc = "".join(str((int(s[0]) - i) % 10) for i in range(len(s)))
        if len(s) >= 4 and s in (asc, desc):   # e.g. "1234", "9876"
            return True
    return False


def fetch(url, retries=6):
    """Small fetch (index files) fully into memory with retry/backoff."""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except Exception as e:
            last = e
            wait = min(60, 3 * (2 ** i))
            print(f"    retry {i+1}/{retries} after {type(e).__name__}: {e} "
                  f"(waiting {wait}s)", flush=True)
            time.sleep(wait)
    raise last


def fetch_bytes(url, fname, attempts=8):
    """Streams one constituent file fully into memory (never to disk) with
    retry/backoff, logging progress as it goes so a 280MB+ file's download
    is visible in the run log rather than looking hung. No caching, no
    resume-from-partial -- a failed attempt just retries from zero; that is
    the accepted trade-off of never persisting a partial file to disk."""
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=300) as r:
                total = r.length
                chunks = []
                got = 0
                next_log_mb = 50
                while True:
                    chunk = r.read(1 << 20)  # 1 MB
                    if not chunk:
                        break
                    chunks.append(chunk)
                    got += len(chunk)
                    if got >= next_log_mb * (1 << 20):
                        print(f"    {fname}: {got / (1<<20):.0f} MB received...", flush=True)
                        next_log_mb += 50
                if total is not None and got < total:
                    raise IOError(f"short read: got {got:,} of {total:,} bytes")
                return b"".join(chunks)
        except Exception as e:
            last = e
            wait = min(60, 3 * (2 ** i))
            print(f"    retry {i+1}/{attempts} after {type(e).__name__}: {e} "
                  f"(waiting {wait}s)", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Failed to fully download {url} after {attempts} attempts: {last}")


def resources_of(raw):
    """Yield each FHIR resource from a Bundle (or a bare list/object)."""
    data = json.loads(raw)
    if isinstance(data, dict) and data.get("resourceType") == "Bundle":
        for e in data.get("entry", []):
            r = e.get("resource", e)
            if isinstance(r, dict):
                yield r
    elif isinstance(data, list):
        for r in data:
            if isinstance(r, dict):
                yield r
    elif isinstance(data, dict):
        yield data


def find_refs(node, path=""):
    """Recursively yield (json_path, reference_string) for every 'reference' field."""
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            if k == "reference" and isinstance(v, str):
                yield p, v
            else:
                yield from find_refs(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from find_refs(v, f"{path}[{i}]")


def field_root(path):
    """'location[0].reference' -> 'location'; 'coverageArea[3].reference' -> 'coverageArea'.
    Different fields mean different things (location vs coverageArea), so they are
    reported separately rather than collapsed into one source->target row."""
    p = re.sub(r"\[\d+\]", "", path)
    if p.endswith(".reference"):
        p = p[: -len(".reference")]
    return p or "reference"


def parse_ref(ref):
    """Return (type, id) for a resolvable reference, else None for contained/urn refs."""
    if not ref or ref.startswith("#") or ref.startswith("urn:"):
        return None
    m = REF_RE.search(ref.strip())
    if not m:
        return None
    return m.group("type"), m.group("id")


def run(contract, index_url):
    print(f"\n=== {contract} ===", flush=True)
    idx = json.loads(fetch(index_url))
    urls = idx.get("provider_urls", [])
    print(f"{len(urls)} files (last_updated={idx.get('last_updated')})", flush=True)

    files = []  # (fname, category, part)
    for url in urls:
        fname = url.rsplit("/", 1)[-1]
        m = FNAME_RE.search(fname)
        files.append((url, fname,
                      m.group("category") if m else "unknown",
                      int(m.group("part")) if m else 0))

    # ---- Single pass per file: download into memory, extract everything
    # needed, then drop the raw bytes/parsed JSON before moving to the next
    # file. Nothing is written to disk and no file is downloaded twice --
    # references are collected here as lightweight tuples and checked for
    # connectivity afterward, once every file's existing-id set is known.
    print("Downloading + indexing (single pass per file, nothing written to disk) ...", flush=True)
    existing = set()
    raw_type_counts = {}          # includes duplicate ids (raw entry count)
    ext_placeholder_by_file = {}  # fname -> count of resources with resource-placeholder ext
    all_refs = []                 # (src_type, src_id, fname, field, ref) -- checked after this loop

    for url, fname, category, part in files:
        raw = fetch_bytes(url, fname)
        n = 0
        for r in resources_of(raw):
            rt, rid = r.get("resourceType"), r.get("id")
            src_type, src_id = rt or "", str(rid) if rid is not None else ""
            if rt and rid is not None and str(rid) != "":
                existing.add((rt, str(rid)))
                raw_type_counts[rt] = raw_type_counts.get(rt, 0) + 1
            if has_placeholder_extension(r):
                ext_placeholder_by_file[fname] = ext_placeholder_by_file.get(fname, 0) + 1
            for field, ref in find_refs(r):
                all_refs.append((src_type, src_id, fname, field, ref))
            n += 1
        del raw  # drop this file's bytes/parsed JSON before the next one
        print(f"  {fname}: {n}", flush=True)

    if ext_placeholder_by_file:
        print(f"\nResources marked with resource-placeholder extension, by file:")
        for fname in sorted(ext_placeholder_by_file):
            print(f"  {fname}: {ext_placeholder_by_file[fname]:,}")
        print(f"Total: {sum(ext_placeholder_by_file.values()):,}")
    # Unique count per type = correct denominator for "% of source affected".
    type_counts = {}
    for t, _ in existing:
        type_counts[t] = type_counts.get(t, 0) + 1
    published_types = set(type_counts)
    dups = sum(raw_type_counts[t] - type_counts.get(t, 0) for t in raw_type_counts)
    print(f"Indexed {len(existing)} unique resources"
          f"{f' ({dups} duplicate ids collapsed)' if dups else ''}.", flush=True)

    # ---- Check every collected reference against the existing-id set ----
    # (no file access here -- all_refs was already extracted above)
    print("Checking references ...", flush=True)
    dangling = []                 # source_type, source_id, file, field, target_type, target_id
    affected = {}                 # (src_type, field, tgt_type) -> source ids with >=1 broken ref
    ref_totals = {}               # (src_type, field, tgt_type) -> total refs seen
    src_with_any_ref = {}         # (src_type, field, tgt_type) -> source ids having such a ref
    bad_ref_counts = {}           # (src_type, field, tgt_type) -> broken ref count
    placeholder_refs = []         # src_type, src_id, file, field, target_type, target_id, connected(bool)
    placeholder_connected = {}    # (src_type, field, tgt_type) -> count where placeholder id resolved
    placeholder_total = {}        # (src_type, field, tgt_type) -> count of placeholder-looking ids seen

    for src_type, src_id, fname, field, ref in all_refs:
        parsed = parse_ref(ref)
        if not parsed:
            continue
        tgt_type, tgt_id = parsed
        key = (src_type, field_root(field), tgt_type)
        ref_totals[key] = ref_totals.get(key, 0) + 1
        src_with_any_ref.setdefault(key, set()).add(src_id)
        connected = (tgt_type, tgt_id) in existing
        if not connected:
            dangling.append([src_type, src_id, fname, field, tgt_type, tgt_id, ref])
            affected.setdefault(key, set()).add(src_id)
            bad_ref_counts[key] = bad_ref_counts.get(key, 0) + 1
        if is_placeholder_id(tgt_id):
            placeholder_refs.append([src_type, src_id, fname, field, tgt_type, tgt_id,
                                      "yes" if connected else "no"])
            placeholder_total[key] = placeholder_total.get(key, 0) + 1
            if connected:
                placeholder_connected[key] = placeholder_connected.get(key, 0) + 1

    # ---- Write per-reference detail ----
    with open(f"dangling_refs_{contract}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source_type", "source_id", "source_file", "field_path",
                    "target_type", "target_id", "raw_reference"])
        w.writerows(dangling)

    # ---- Write placeholder-id detail (dummy-looking ids, connected or not) ----
    with open(f"placeholder_refs_{contract}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source_type", "source_id", "source_file", "field_path",
                    "target_type", "target_id", "connected"])
        w.writerows(placeholder_refs)

    # ---- Write summary (per source.field -> target) ----
    rows = []
    for key in sorted(set(list(ref_totals) + list(affected))):
        src_type, field, tgt_type = key
        n_bad_refs = bad_ref_counts.get(key, 0)
        n_affected = len(affected.get(key, ()))
        n_src_total = type_counts.get(src_type, 0)
        n_src_with_ref = len(src_with_any_ref.get(key, ()))
        pct_of_all = (100.0 * n_affected / n_src_total) if n_src_total else 0.0
        pct_of_linked = (100.0 * n_affected / n_src_with_ref) if n_src_with_ref else 0.0
        # "yes" = target type is published but specific ids are missing;
        # "no"  = that whole resource type was never published in this contract.
        tgt_published = "yes" if tgt_type in published_types else "no"
        rows.append([src_type, field, tgt_type, tgt_published, ref_totals.get(key, 0),
                     n_bad_refs, n_affected, n_src_total, f"{pct_of_all:.2f}",
                     n_src_with_ref, f"{pct_of_linked:.2f}"])

    with open(f"dangling_summary_{contract}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source_type", "field", "target_type", "target_type_published",
                    "total_refs", "dangling_refs",
                    "affected_source_resources", "total_source_resources", "pct_of_all_source",
                    "source_resources_with_this_ref", "pct_of_linked_source"])
        w.writerows(rows)

    # ---- Console report ----
    print(f"\n--- {contract} dangling references (by field) ---")
    hdr = (f"{'SOURCE.FIELD':<42} {'TARGET':<18} {'PUB':>4} {'REFS':>10} "
           f"{'BROKEN':>10} {'AFFECTED':>9} {'%TYPE':>7}")
    print(hdr)
    print("-" * len(hdr))
    for (src_type, field, tgt_type, tgt_pub, total, bad,
         aff, src_total, pct, _, _) in rows:
        if bad:
            print(f"{src_type + '.' + field:<42} {tgt_type:<18} {tgt_pub:>4} {total:>10,} "
                  f"{bad:>10,} {aff:>9,} {pct:>6}%")
    clean = [r for r in rows if not r[5]]
    if clean:
        print("\nClean (no dangling refs):")
        for src_type, field, tgt_type, *_ in clean:
            print(f"  {src_type}.{field} -> {tgt_type}: all resolve")
    missing_types = sorted({r[2] for r in rows if r[3] == "no" and r[5]})
    if missing_types:
        print(f"\nTarget types referenced but NEVER published: {', '.join(missing_types)}")

    if placeholder_refs:
        print(f"\n--- {contract} placeholder-looking target ids ---")
        phdr = f"{'SOURCE.FIELD':<42} {'TARGET':<18} {'PLACEHOLDER':>11} {'CONNECTED':>10}"
        print(phdr)
        print("-" * len(phdr))
        for key in sorted(set(list(placeholder_total))):
            src_type, field, tgt_type = key
            tot = placeholder_total.get(key, 0)
            conn = placeholder_connected.get(key, 0)
            print(f"{src_type + '.' + field:<42} {tgt_type:<18} {tot:>11,} {conn:>10,}")

        # Roll up by target resource type, across every field that references it.
        by_type_total, by_type_conn = {}, {}
        for row in placeholder_refs:
            _, _, _, _, tgt_type, _, connected = row
            by_type_total[tgt_type] = by_type_total.get(tgt_type, 0) + 1
            if connected == "yes":
                by_type_conn[tgt_type] = by_type_conn.get(tgt_type, 0) + 1
        print(f"\nPlaceholder counts by resource type (all fields combined):")
        thdr = f"{'RESOURCE TYPE':<20} {'PLACEHOLDER':>11} {'CONNECTED':>10} {'DANGLING':>9}"
        print(thdr)
        print("-" * len(thdr))
        for tgt_type in sorted(by_type_total):
            tot = by_type_total[tgt_type]
            conn = by_type_conn.get(tgt_type, 0)
            print(f"{tgt_type:<20} {tot:>11,} {conn:>10,} {tot - conn:>9,}")

        n_conn = sum(placeholder_connected.values())
        n_not_conn = len(placeholder_refs) - n_conn
        print(f"\nTotal placeholder-looking ids: {len(placeholder_refs):,} "
              f"({n_conn:,} still resolve to a real resource, "
              f"{n_not_conn:,} are dangling)")

        if n_not_conn:
            print(f"\nNOT CONNECTED placeholder ids (dangling): {n_not_conn:,}")
            not_conn_by_type = {}
            for row in placeholder_refs:
                _, _, _, _, tgt_type, _, connected = row
                if connected == "no":
                    not_conn_by_type[tgt_type] = not_conn_by_type.get(tgt_type, 0) + 1
            for tgt_type in sorted(not_conn_by_type):
                print(f"  {tgt_type}: {not_conn_by_type[tgt_type]:,}")

        # Placeholder count per source JSON file, standalone (no connected/dangling split).
        by_file = {}
        for row in placeholder_refs:
            fname = row[2]
            by_file[fname] = by_file.get(fname, 0) + 1
        print(f"\nPlaceholder count by source file:")
        for fname in sorted(by_file):
            print(f"  {fname}: {by_file[fname]:,}")

        # End-to-end: InsurancePlan -> Organization specifically (its own chain,
        # not mixed in with every other source type that also references Organization).
        ip_org = [r for r in placeholder_refs
                  if r[0] == "InsurancePlan" and r[4] == "Organization"]
        if ip_org:
            ip_conn = sum(1 for r in ip_org if r[6] == "yes")
            ip_not_conn = len(ip_org) - ip_conn
            print(f"\nEnd-to-end: InsurancePlan -> Organization placeholder ids: "
                  f"{len(ip_org):,} total ({ip_conn:,} connected, {ip_not_conn:,} not connected)")
            for src_type, src_id, fname, field, tgt_type, tgt_id, connected in ip_org:
                print(f"  InsurancePlan/{src_id} --{field}--> Organization/{tgt_id} "
                      f"[{('connected' if connected == 'yes' else 'NOT CONNECTED')}]")

    print(f"\nTotal dangling references: {len(dangling):,}")
    print(f"Wrote: dangling_refs_{contract}.csv, dangling_summary_{contract}.csv"
          + (f", placeholder_refs_{contract}.csv" if placeholder_refs else ""))
    return len(dangling)


# CLI: python check_refs.py [CONTRACT]
#   CONTRACT  run just one contract (default: all)
# There is no cache and no --fresh/--cache flag anymore: every run streams
# each file fresh from the live endpoint straight into memory and never
# writes it to disk, so there is nothing to invalidate or reuse.
if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    positional = [a for a in args if not a.startswith("-")]
    arg = positional[0].upper() if positional else None

    if arg:
        if arg not in CONTRACTS:
            print(f"Unknown contract '{arg}'. Choose from: {', '.join(CONTRACTS)}")
            sys.exit(1)
        targets = {arg: CONTRACTS[arg]}
    else:
        targets = CONTRACTS

    grand_total = 0
    for c, u in targets.items():
        grand_total += run(c, u)
    if len(targets) > 1:
        print(f"\n==== ALL CONTRACTS: {grand_total:,} total dangling references ====")
