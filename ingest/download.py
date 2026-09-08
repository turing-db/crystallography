"""Acquire the COD slice: metadata from COD's MySQL, CIFs over rsync.

Two deliberate choices, both about provenance and about not being rude:

* **rsync, not HTTP.** COD's `robots.txt` is `Disallow: /`, and the published
  release tarball is 19 months stale (`LAST_RELEASE.txt` reads
  `297631 2025.02.09`, and there is no 2026 release). The rsync tree is live.
  We fetch *only the slice* with `--files-from`, so this pulls ~2 GB rather
  than the 109 GB the full tree would cost.

* **A manifest that can be audited.** CCDC will ask which COD this was built
  from. We record the published release revision, the live SVN revision at
  download time, the exact journal strings matched, the size cap applied, and
  the full sorted ID list with a checksum.

Usage
-----
    uv run python -m ingest.download --dataset slice
    uv run python -m ingest.download --dataset slice --limit 500   # dev subset
    uv run python -m ingest.download --dataset slice --metadata-only
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .cod_journals import JOURNALS, JOURNALS_BY_KEY, all_variants, journal_key_for

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

RSYNC_CIF_MODULE = "rsync://www.crystallography.net/cif/"
SVN_URL = "svn://www.crystallography.net/cod"
LAST_RELEASE_URL = "https://www.crystallography.net/archives/LAST_RELEASE.txt"

# COD's public read-only metadata mirror, documented on the COD wiki.
MYSQL_HOST = "sql.crystallography.net"
MYSQL_USER = "cod_reader"
MYSQL_DB = "cod"

#: Skip CIFs larger than this.
#:
#: Measured against the slice itself (89,947 files, 8.49 GB total) rather than
#: against all of COD, because the slice is much better behaved than the full
#: database: p50 17 kB, p75 25 kB, p90 80 kB, but p95 506 kB and p99 1.7 MB.
#: The tail is thin and consists of very large asymmetric units (big MOFs,
#: entries carrying embedded reflection data) which are the least representative
#: of CCDC's small-molecule remit and by far the slowest to symmetry-expand.
#:
#:      cap  128 kB -> 81,661 (90.8%)  1.50 GB
#:      cap  512 kB -> 85,492 (95.0%)  2.62 GB   <- default
#:      cap 1024 kB -> 87,858 (97.7%)  4.27 GB
#:      no cap      -> 89,947 (100%)   8.49 GB
#:
#: 512 kB keeps 95% of the slice for a third of the bytes, leaving disk for the
#: derived JSONL and the loaded graphs. Every skipped ID is recorded in the
#: manifest so the exclusion is auditable rather than silent.
DEFAULT_MAX_CIF_BYTES = 524_288

#: Columns pulled from `cod.data`. Names verified against the live schema.
#: Note `vol` is the cell volume; `volume` is the journal volume.
METADATA_COLUMNS: tuple[str, ...] = (
    "file",
    # bibliography
    "journal", "year", "volume", "issue", "firstpage", "lastpage", "doi",
    "authors", "title",
    # symmetry
    "sg", "sgHall", "sgNumber",
    # cell
    "a", "b", "c", "alpha", "beta", "gamma", "vol",
    "siga", "sigb", "sigc", "sigalpha", "sigbeta", "siggamma",
    "Z", "Zprime",
    # quality
    "Rall", "Robs", "Rref", "wRall", "wRobs", "wRref",
    "gofall", "gofobs", "gofgt",
    # conditions
    "celltemp", "sigcelltemp", "diffrtemp", "sigdiffrtemp",
    "cellpressure", "sigcellpressure", "diffrpressure", "sigdiffrpressure",
    "thermalhist", "pressurehist",
    # composition / identity
    "formula", "calcformula", "cellformula", "nel",
    "commonname", "chemname", "mineral", "compoundsource",
    # provenance and revision history -- this is what feeds the SUPERSEDES layer
    "duplicateof", "optimal", "status", "flags", "method",
    "radiation", "wavelength", "radType",
    "acce_code", "svnrevision", "date", "time", "onhold",
)


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------


@dataclass
class Provenance:
    """Everything needed to say exactly which COD this dataset came from."""

    downloaded_at: str
    #: from LAST_RELEASE.txt, e.g. "297631"
    published_release_revision: str | None
    published_release_date: str | None
    #: live SVN revision of the rsync tree at download time
    live_svn_revision: str | None
    #: highest `data.svnrevision` seen across the fetched rows. Recorded because
    #: the box has no svn client, and this pins the metadata snapshot using the
    #: data itself rather than an external call.
    metadata_max_svnrevision: int | None = None
    rsync_module: str = RSYNC_CIF_MODULE
    mysql_host: str = MYSQL_HOST
    note: str = ""


@dataclass
class Manifest:
    """The audit record for one built dataset."""

    dataset: str
    provenance: Provenance
    journals: dict[str, dict[str, Any]]
    max_cif_bytes: int | None
    limit: int | None
    counts: dict[str, int] = field(default_factory=dict)
    ids_file: str = ""
    ids_sha256: str = ""
    metadata_file: str = ""
    tool_versions: dict[str, str] = field(default_factory=dict)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=False) + "\n")


def _run(cmd: Sequence[str], timeout: int = 120) -> tuple[int, str, str]:
    proc = subprocess.run(
        list(cmd), capture_output=True, text=True, timeout=timeout, check=False
    )
    return proc.returncode, proc.stdout, proc.stderr


def fetch_release_info() -> tuple[str | None, str | None]:
    """Read COD's published release stamp: `<svn_revision> <YYYY.MM.DD>`."""
    try:
        import httpx

        text = httpx.get(LAST_RELEASE_URL, timeout=30).text
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not fetch LAST_RELEASE.txt: {exc}")
        return None, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            return parts[0], parts[1]
    return None, None


def fetch_live_svn_revision() -> str | None:
    """Global SVN revision of the live tree, so an rsync build is still pinned."""
    if shutil.which("svn") is None:
        print("  ! svn client not installed; live revision will be null")
        return None
    code, out, _ = _run(["svn", "info", "--show-item", "revision", SVN_URL], timeout=120)
    return out.strip() if code == 0 and out.strip() else None


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------


def fetch_metadata(journal_keys: Sequence[str]) -> list[dict[str, Any]]:
    """Pull `cod.data` rows for the target journals from COD's public MySQL.

    Deliberately does NOT apply COD's default web filter. We want the
    duplicate/retracted/errors rows too, because `duplicateof`, `optimal` and
    `status` are exactly the columns that feed the SUPERSEDES / revision layer
    of the graph. Filtering happens later, in build_graph, where it is visible.
    """
    try:
        import pymysql
    except ImportError:  # pragma: no cover
        print("pymysql is required: uv add pymysql", file=sys.stderr)
        raise

    variants = [v for k in journal_keys for v in JOURNALS_BY_KEY[k].variants]
    placeholders = ", ".join(["%s"] * len(variants))
    cols = ", ".join(f"`{c}`" for c in METADATA_COLUMNS)
    # BINARY is deliberately NOT used: the stored spellings differ in case
    # ("Crystal growth & design"), so we want the collation's case-insensitive
    # comparison here and normalise properly in journal_key_for().
    sql = f"SELECT {cols} FROM data WHERE journal IN ({placeholders})"

    print(f"  connecting to {MYSQL_USER}@{MYSQL_HOST}/{MYSQL_DB} ...")
    conn = pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        database=MYSQL_DB,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=30,
        read_timeout=600,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, variants)
            rows = cur.fetchall()
    finally:
        conn.close()
    print(f"  fetched {len(rows):,} metadata rows")
    return list(rows)


# --------------------------------------------------------------------------
# CIF paths
# --------------------------------------------------------------------------


def cod_cif_relpath(cod_id: int | str) -> str:
    """Relative path of a CIF inside the COD tree.

    COD nests by the 1st, 2nd-3rd and 4th-5th digits of the 7-digit COD ID:
    `1000000` -> `1/00/00/1000000.cif`. Verified uniform: all 535,041 CIFs sit
    at exactly this depth.
    """
    sid = str(cod_id).strip()
    if not sid.isdigit() or len(sid) != 7:
        raise ValueError(f"COD ID must be 7 digits, got {cod_id!r}")
    return f"{sid[0]}/{sid[1:3]}/{sid[3:5]}/{sid}.cif"


def rsync_slice(
    relpaths: Iterable[str],
    dest: Path,
    max_bytes: int | None,
    dry_run: bool = False,
) -> tuple[int, str]:
    """Fetch exactly the listed CIFs with one rsync call.

    `--files-from` keeps this to the slice instead of the whole 109 GB module.
    rsync is inherently resumable, so re-running only transfers what changed --
    which is also how retractions and re-refinements show up on a later run.
    """
    dest.mkdir(parents=True, exist_ok=True)
    listing = dest.parent / "manifest" / "_rsync_files.txt"
    listing.parent.mkdir(parents=True, exist_ok=True)
    paths = sorted(set(relpaths))
    listing.write_text("\n".join(paths) + "\n")

    cmd = [
        "rsync", "-a", "--partial", "--stats", "--no-motd",
        f"--files-from={listing}",
    ]
    if max_bytes:
        # rsync skips oversize files itself, so the long tail never crosses the
        # wire. Skipped files are reported and counted in the manifest.
        cmd.append(f"--max-size={max_bytes}")
    if dry_run:
        cmd.append("--dry-run")
    cmd += [RSYNC_CIF_MODULE, str(dest)]

    print(f"  rsync {len(paths):,} files -> {dest}"
          f"{f' (max-size={max_bytes})' if max_bytes else ''}"
          f"{' [DRY RUN]' if dry_run else ''}")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(proc.stderr[-3000:], file=sys.stderr)
    return proc.returncode, proc.stdout


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tool_versions() -> dict[str, str]:
    out: dict[str, str] = {"python": sys.version.split()[0]}
    code, so, _ = _run(["rsync", "--version"], timeout=30)
    if code == 0:
        out["rsync"] = so.splitlines()[0].strip()
    for mod in ("gemmi", "rdkit", "turingdb"):
        try:
            m = __import__(mod)
            out[mod] = str(getattr(m, "__version__", "?"))
        except Exception:  # noqa: BLE001
            out[mod] = "not installed"
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default="slice", help="dataset name (manifest key)")
    ap.add_argument("--out", default="data", type=Path, help="output root")
    ap.add_argument(
        "--journals", default="",
        help="comma-separated journal keys; default is all three slice journals",
    )
    ap.add_argument(
        "--max-cif-bytes", type=int, default=DEFAULT_MAX_CIF_BYTES,
        help="skip CIFs larger than this (0 disables the cap)",
    )
    ap.add_argument(
        "--limit", type=int, default=None,
        help="dev subset: N ids sampled deterministically across the slice",
    )
    ap.add_argument("--seed", type=int, default=20260903, help="sampling seed for --limit")
    ap.add_argument("--metadata-only", action="store_true", help="skip the rsync")
    ap.add_argument("--dry-run", action="store_true", help="rsync --dry-run")
    args = ap.parse_args(argv)

    keys = (
        [k.strip() for k in args.journals.split(",") if k.strip()]
        if args.journals
        else [j.key for j in JOURNALS]
    )
    unknown = [k for k in keys if k not in JOURNALS_BY_KEY]
    if unknown:
        ap.error(f"unknown journal keys: {unknown}. known: {list(JOURNALS_BY_KEY)}")

    out = Path(args.out)
    cif_dir = out / "cif"
    man_dir = out / "manifest"
    man_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    print(f"=== COD acquisition: dataset={args.dataset} journals={keys} ===")

    print("[1/4] provenance")
    rel_rev, rel_date = fetch_release_info()
    print(f"  published release: revision={rel_rev} date={rel_date}")
    live_rev = fetch_live_svn_revision()
    print(f"  live svn revision: {live_rev}")
    prov = Provenance(
        downloaded_at=datetime.now(timezone.utc).isoformat(),
        published_release_revision=rel_rev,
        published_release_date=rel_date,
        live_svn_revision=live_rev,
        note=(
            "CIFs come from the live rsync tree, not the published tarball: the "
            "newest tarball is cod-rev297631-2025.02.07 and there is no 2026 "
            "release, so the tarball would be ~19 months stale."
        ),
    )

    print("[2/4] metadata")
    rows = fetch_metadata(keys)

    # tag each row with our journal key and drop anything that slipped through
    kept: list[dict[str, Any]] = []
    per_journal: dict[str, int] = {k: 0 for k in keys}
    unmatched = 0
    for row in rows:
        jkey = journal_key_for(row.get("journal"))
        if jkey is None or jkey not in per_journal:
            unmatched += 1
            continue
        row["_journal_key"] = jkey
        per_journal[jkey] += 1
        kept.append(row)
    if unmatched:
        print(f"  ! {unmatched} rows matched the SQL filter but not our variant table")
    for k, n in per_journal.items():
        exp = JOURNALS_BY_KEY[k].expected_rows
        flag = "" if abs(n - exp) <= max(50, exp * 0.02) else "   <-- differs from expected"
        print(f"    {k:<24} {n:>7,}  (expected ~{exp:,}){flag}")

    revs = [int(r["svnrevision"]) for r in kept if str(r.get("svnrevision") or "").isdigit()]
    prov.metadata_max_svnrevision = max(revs) if revs else None
    print(f"  metadata max svnrevision: {prov.metadata_max_svnrevision}")

    kept.sort(key=lambda r: int(r["file"]))
    if args.limit and args.limit < len(kept):
        # Sample, do not truncate. COD IDs are assigned chronologically, so the
        # first N ids are the oldest deposits -- a biased subset with unusual
        # file conventions and much larger CIFs than the slice average. A seeded
        # sample keeps dev subsets representative and still reproducible.
        import random

        kept = random.Random(args.seed).sample(kept, args.limit)
        kept.sort(key=lambda r: int(r["file"]))
        print(f"  --limit {args.limit}: sampled {len(kept):,} ids (seed={args.seed})")

    ids = [int(r["file"]) for r in kept]
    ids_file = man_dir / f"{args.dataset}_ids.txt"
    ids_file.write_text("\n".join(str(i) for i in ids) + "\n")

    meta_file = man_dir / f"{args.dataset}_metadata.csv"
    with meta_file.open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=list(METADATA_COLUMNS) + ["_journal_key"], extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(kept)
    print(f"  wrote {meta_file} ({meta_file.stat().st_size / 1e6:.1f} MB)")

    # revision-layer signal, reported now because it drives Stage 3's provenance
    n_dup = sum(1 for r in kept if r.get("duplicateof"))
    n_retracted = sum(1 for r in kept if (r.get("status") or "") == "retracted")
    n_errors = sum(1 for r in kept if (r.get("status") or "") == "errors")
    n_theoretical = sum(1 for r in kept if (r.get("method") or "") == "theoretical")
    print(f"  revision signal: {n_dup:,} duplicateof, {n_retracted:,} retracted, "
          f"{n_errors:,} errors, {n_theoretical:,} theoretical")

    print("[3/4] CIFs")
    n_before = sum(1 for _ in cif_dir.rglob("*.cif")) if cif_dir.exists() else 0
    if args.metadata_only:
        print("  --metadata-only: skipping rsync")
        rsync_out = ""
    else:
        relpaths = []
        bad = 0
        for i in ids:
            try:
                relpaths.append(cod_cif_relpath(i))
            except ValueError:
                bad += 1
        if bad:
            print(f"  ! {bad} ids were not 7 digits and were skipped")
        code, rsync_out = rsync_slice(
            relpaths, cif_dir, args.max_cif_bytes or None, dry_run=args.dry_run
        )
        if code != 0:
            print(f"  ! rsync exited {code}")
        for line in rsync_out.splitlines():
            if re.search(r"(Number of files|Total file size|Total transferred)", line):
                print(f"    {line.strip()}")

    # Work out exactly which requested ids did NOT land, so the exclusion is
    # auditable. CCDC will ask why a given structure is absent, and "it exceeded
    # the size cap" is only a defensible answer if we can name the ids.
    present: set[int] = set()
    bytes_on_disk = 0
    if cif_dir.exists():
        for p in cif_dir.rglob("*.cif"):
            bytes_on_disk += p.stat().st_size
            if p.stem.isdigit():
                present.add(int(p.stem))
    n_after = len(present)
    on_disk = sum(1 for i in ids if i in present)
    absent = [i for i in ids if i not in present]
    missing = len(absent)
    if absent and not args.metadata_only:
        skipped_file = man_dir / f"{args.dataset}_skipped_ids.txt"
        skipped_file.write_text("\n".join(str(i) for i in absent) + "\n")
        print(f"  wrote {skipped_file} ({len(absent):,} ids not retrieved)")

    print("[4/4] manifest")
    manifest = Manifest(
        dataset=args.dataset,
        provenance=prov,
        journals={
            k: {
                "display": JOURNALS_BY_KEY[k].display,
                "variants_matched": list(JOURNALS_BY_KEY[k].variants),
                "rows": per_journal[k],
                "note": JOURNALS_BY_KEY[k].note,
            }
            for k in keys
        },
        max_cif_bytes=args.max_cif_bytes or None,
        limit=args.limit,
        counts={
            "metadata_rows": len(kept),
            "ids_requested": len(ids),
            "cifs_on_disk": on_disk,
            "cifs_added_this_run": max(0, n_after - n_before),
            "cifs_missing_or_oversize": max(0, missing),
            "cifs_total_in_tree": n_after,
            "bytes_on_disk": bytes_on_disk,
            "duplicateof": n_dup,
            "status_retracted": n_retracted,
            "status_errors": n_errors,
            "method_theoretical": n_theoretical,
        },
        ids_file=str(ids_file),
        ids_sha256=_sha256(ids_file),
        metadata_file=str(meta_file),
        tool_versions=_tool_versions(),
    )
    man_path = man_dir / f"{args.dataset}_manifest.json"
    manifest.write(man_path)

    print(f"  ids           : {len(ids):,}")
    print(f"  cifs on disk  : {on_disk:,}  ({bytes_on_disk / 1e9:.2f} GB)")
    print(f"  missing/oversz: {max(0, missing):,}")
    print(f"  manifest      : {man_path}")
    print(f"=== done in {time.time() - t0:.1f}s ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
