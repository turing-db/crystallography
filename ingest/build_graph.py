"""Build the crystallography graph and emit it as JSONL for TuringDB.

Output format is `LOAD JSONL`'s, which is Neo4j APOC's `apoc.export.json.all`
shape: one JSON object per line, nodes and relationships interleaved, node ids
and relationship ids each starting at 0 and incrementing with no gaps.

Schema note -- three names differ from the obvious ones, and the reasons are
enforced by the engine rather than stylistic:

  * `Fragment.fragment_type`, not `type`  -- `type` is a reserved token.
  * `Publication.journal_volume`, not `volume` -- property types are GLOBAL in
    TuringDB, so a String journal volume collides with Structure's Double cell
    volume.
  * `has_*` booleans accompany every optional numeric field, because
    `IS NULL` / `IS NOT NULL` do not work, and COD is full of missing values
    which the spec forbids fabricating.

Usage:
    uv run python -m ingest.build_graph --dataset slice --limit 20000 --workers 16
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from . import chemistry as ch
from .cod_journals import JOURNALS_BY_KEY
from .contacts import detect_contacts
from .download import cod_cif_relpath
from .fragments import FRAGMENTS_BY_NAME
from .perception import perceive_component
from .periodic import PeriodicNeighbours, asymmetric_positions
from .solvents import assign_roles, solvent_name
from .structure import (
    _max_bond_cutoff,
    build_atoms,
    component_instances,
    detect_bonds,
    find_components,
    read_structure,
)

MAX_SITES = 400          # skip pathologically large asymmetric units
#: Wall-clock budget for one structure. COD contains a long tail of entries that
#: are individually pathological -- unusual symmetry, extreme disorder, or a
#: connectivity that sends one of the graph walks into a very large search. A
#: watchdog is the honest way to handle that: the structure is skipped and
#: COUNTED, rather than the whole ingest appearing to hang on entry 11,000.
PER_STRUCTURE_TIMEOUT_S = 20
#: Wall-clock budget for the contact search of ONE structure. Exceeding it
#: truncates that structure's contact list and flags it, rather than stalling
#: the ingest (see ingest/contacts.py).
CONTACT_TIME_BUDGET_S = 6.0
#: n_sites x n_symmetry_operations, i.e. atoms in the full unit cell.
MAX_UNIT_CELL_ATOMS = 4000


# --------------------------------------------------------------------------
# per-structure worker
# --------------------------------------------------------------------------


class _StructureTimeout(Exception):
    pass


def _alarm(_signum: int, _frame: object) -> None:
    raise _StructureTimeout()


def process_structure(args: tuple[str, int]) -> dict[str, Any] | None:
    """Parse one CIF into a plain dict, under a wall-clock watchdog."""
    path, cod_id = args
    armed = False
    try:
        # SIGALRM only exists on the main thread of a Unix process, which is
        # exactly how the single-worker path runs. Guarded so it degrades to
        # "no watchdog" rather than crashing anywhere else.
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(PER_STRUCTURE_TIMEOUT_S)
        armed = True
    except (ValueError, AttributeError, OSError):
        armed = False
    try:
        got = read_structure(path)
        if got is None:
            return {"cod_id": cod_id, "skipped": "unreadable_or_no_spacegroup"}
        st, sg = got
        atoms = build_atoms(st, cod_id)
        if not atoms or len(atoms) > MAX_SITES:
            return {"cod_id": cod_id, "skipped": "too_many_sites"}

        # Bound the symmetry expansion up front. n_sites x n_operations is the
        # unit-cell atom count; a cubic group has up to 192 operations, so this
        # is what separates an ordinary organic from an hour-long worker.
        n_ops = len(list(sg.operations()))
        if len(atoms) * n_ops > MAX_UNIT_CELL_ATOMS:
            return {"cod_id": cod_id, "skipped": "expansion_too_large"}

        fr = np.array([[a.fract_x, a.fract_y, a.fract_z] for a in atoms])
        nb_bond = PeriodicNeighbours(st, sg, fr, _max_bond_cutoff(atoms))
        if nb_bond.truncated:
            return {"cod_id": cod_id, "skipped": "expansion_too_large"}
        bonds, bnotes = detect_bonds(st, sg, atoms, nb_bond)
        comps, polymeric, cnotes = find_components(st, sg, atoms, bonds)

        nb_con = PeriodicNeighbours(st, sg, fr, ch.MAX_CONTACT_SEARCH_RADIUS)
        pos = asymmetric_positions(st.cell, fr)
        contacts, cstats = detect_contacts(
            st, sg, atoms, bonds, nb_con, pos, comps,
            time_budget_s=CONTACT_TIME_BUDGET_S,
        )

        comp_rows: list[dict[str, Any]] = []
        for ci, sites in enumerate(comps):
            is_poly = ci in polymeric
            if is_poly:
                counts: Counter[str] = Counter(atoms[i].element for i in sites)
                from .structure import hill_formula

                comp_rows.append({
                    "index": ci, "sites": sites, "formula": hill_formula(counts),
                    "n_atoms": len(sites), "n_heavy": sum(
                        v for k, v in counts.items() if k != "H"
                    ),
                    "inchikey": None, "fallback_key": None, "charge": None,
                    "fragments": [], "is_polymeric": True, "error": "",
                })
                continue
            inst = component_instances(sg, atoms, bonds, sites)
            p = perceive_component(st.cell, atoms, bonds, inst, sg)
            comp_rows.append({
                "index": ci, "sites": sites, "formula": p.formula,
                "n_atoms": p.n_atoms, "n_heavy": p.n_heavy,
                "inchikey": p.inchikey, "fallback_key": p.fallback_key,
                "charge": p.charge, "fragments": p.fragments,
                "is_polymeric": False, "error": p.error,
            })

        roles = assign_roles(
            [(c["formula"], c["n_heavy"], c["n_atoms"]) for c in comp_rows]
        )
        for c, r in zip(comp_rows, roles):
            c["role"] = r
            c["is_solvent"] = r == "solvent"
            c["name"] = solvent_name(c["formula"])

        site_to_comp = {s: c["index"] for c in comp_rows for s in c["sites"]}

        return {
            "cod_id": cod_id,
            "spacegroup_hm": st.spacegroup_hm or sg.xhm(),
            "spacegroup_number": sg.number,
            "crystal_system": sg.crystal_system_str(),
            "cell": [st.cell.a, st.cell.b, st.cell.c,
                     st.cell.alpha, st.cell.beta, st.cell.gamma],
            "cell_volume": st.cell.volume,
            "atoms": [
                {"uid": a.uid, "label": a.label, "element": a.element,
                 "fx": a.fract_x, "fy": a.fract_y, "fz": a.fract_z,
                 "occ": a.occupancy, "u_iso": a.u_iso,
                 "comp": site_to_comp.get(a.site_index, -1)}
                for a in atoms
            ],
            "bonds": [
                {"a": b.a_uid, "b": b.b_uid, "length": b.length,
                 "symop": b.symop, "triplet": b.symop_triplet}
                for b in bonds
            ],
            "contacts": [
                {"a": c.a_uid, "b": c.b_uid, "kind": c.kind, "length": c.length,
                 "angle": c.angle, "symop": c.symop, "triplet": c.symop_triplet,
                 "h_inferred": c.h_inferred, "donor": c.donor_uid,
                 "hydrogen": c.hydrogen_uid}
                for c in contacts
            ],
            "components": comp_rows,
            "stats": cstats,
            "notes": bnotes + cnotes,
        }
    except _StructureTimeout:
        return {"cod_id": cod_id,
                "skipped": f"timeout_{PER_STRUCTURE_TIMEOUT_S}s"}
    except Exception as exc:  # noqa: BLE001
        return {"cod_id": cod_id, "skipped": f"error:{type(exc).__name__}:{exc}"[:200]}
    finally:
        if armed:
            signal.alarm(0)


# --------------------------------------------------------------------------
# graph assembly
# --------------------------------------------------------------------------


class Registry:
    """Assigns gap-free sequential ids, which LOAD JSONL requires."""

    def __init__(self) -> None:
        self.nodes: list[dict[str, Any]] = []
        self._index: dict[tuple[str, str], int] = {}

    def node(self, label: str, key: str, props: dict[str, Any]) -> int:
        k = (label, key)
        got = self._index.get(k)
        if got is not None:
            return got
        nid = len(self.nodes)
        self._index[k] = nid
        self.nodes.append({"type": "node", "id": str(nid), "labels": [label],
                           "properties": _clean(props)})
        return nid

    def has(self, label: str, key: str) -> int | None:
        return self._index.get((label, key))


def _clean(props: dict[str, Any]) -> dict[str, Any]:
    """Drop None values; TuringDB has no null literal and IS NULL does not work."""
    return {k: v for k, v in props.items() if v is not None and v != ""}


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "" or str(value).upper() in ("NULL", "\\N", "NAN"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    f = _num(value)
    return int(f) if f is not None else None


def build(
    results: Iterable[dict[str, Any]],
    metadata: dict[int, dict[str, str]],
    out_path: Path,
) -> dict[str, Any]:
    reg = Registry()
    edges: list[dict[str, Any]] = []
    report: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    perception_errors: Counter[str] = Counter()

    def edge(label: str, src: int, tgt: int, props: dict[str, Any] | None = None) -> None:
        edges.append({
            "type": "relationship", "id": str(len(edges)), "label": label,
            "start": {"id": str(src)}, "end": {"id": str(tgt)},
            "properties": _clean(props or {}),
        })

    for res in results:
        if res is None:
            report["skipped_none"] += 1
            continue
        if res.get("skipped"):
            report["skipped"] += 1
            report[f"skip_{res['skipped'].split(':')[0]}"] += 1
            continue
        cod_id = res["cod_id"]
        meta = metadata.get(cod_id, {})
        report["structures"] += 1

        # ---- Structure -------------------------------------------------
        a, b, c, al, be, ga = res["cell"]
        temp = _num(meta.get("celltemp")) or _num(meta.get("diffrtemp"))
        press = _num(meta.get("cellpressure")) or _num(meta.get("diffrpressure"))
        rfac = _num(meta.get("Robs")) or _num(meta.get("Rall")) or _num(meta.get("Rref"))
        zval = _int(meta.get("Z"))
        zprime = _num(meta.get("Zprime"))
        year = _int(meta.get("year"))
        for field_name, value in (("temperature", temp), ("pressure", press),
                                  ("r_factor", rfac), ("Z", zval),
                                  ("Z_prime", zprime), ("year", year)):
            if value is None:
                missing[field_name] += 1

        s_props: dict[str, Any] = {
            "cod_id": cod_id,
            "a": a, "b": b, "c": c, "alpha": al, "beta": be, "gamma": ga,
            "cell_volume": res["cell_volume"],
            "hm_symbol": res["spacegroup_hm"],
            "crystal_system": res["crystal_system"],
            "formula": (meta.get("formula") or "").strip("- ") or None,
            "journal_key": meta.get("_journal_key"),
            "year": year, "Z": zval, "Z_prime": zprime,
            "temperature": temp, "pressure": press, "r_factor": rfac,
            # IS NULL does not work in this dialect, so presence is explicit
            "has_temperature": temp is not None,
            "has_pressure": press is not None,
            "has_r_factor": rfac is not None,
            "has_z_prime": zprime is not None,
            "n_components": len(res["components"]),
            "n_contacts": len(res["contacts"]),
            # honest flag: this structure's contact search hit its time budget,
            # so its contact list is incomplete
            "contacts_truncated": bool(res.get("stats", {}).get("timed_out")),
            "status": (meta.get("status") or "").strip() or None,
            "method": (meta.get("method") or "").strip() or None,
            "has_hydrogens": any(at["element"] == "H" for at in res["atoms"]),
        }
        sid = reg.node("Structure", str(cod_id), s_props)

        # ---- SpaceGroup / Element --------------------------------------
        gid = reg.node("SpaceGroup", str(res["spacegroup_number"]), {
            "hm_symbol": res["spacegroup_hm"],
            "number": res["spacegroup_number"],
            "crystal_system": res["crystal_system"],
        })
        edge("IN_SPACE_GROUP", sid, gid)

        for symbol in sorted({at["element"] for at in res["atoms"] if at["element"]}):
            eid = reg.node("Element", symbol, {
                "symbol": symbol,
                "atomic_number": _atomic_number(symbol),
            })
            edge("CONTAINS_ELEMENT", sid, eid)

        # ---- Atoms ------------------------------------------------------
        atom_ids: dict[str, int] = {}
        for at in res["atoms"]:
            aid = reg.node("Atom", at["uid"], {
                "uid": at["uid"], "label": at["label"], "element": at["element"],
                "fract_x": at["fx"], "fract_y": at["fy"], "fract_z": at["fz"],
                "occupancy": at["occ"], "u_iso": at["u_iso"],
                "has_u_iso": at["u_iso"] is not None,
                "cod_id": cod_id,
            })
            atom_ids[at["uid"]] = aid
            edge("HAS_SITE", sid, aid)
        report["atoms"] += len(res["atoms"])

        for bd in res["bonds"]:
            x, y = atom_ids.get(bd["a"]), atom_ids.get(bd["b"])
            if x is None or y is None:
                continue
            edge("BONDED_TO", x, y, {
                "length": bd["length"], "symop": bd["symop"],
                "symop_triplet": bd["triplet"],
            })
        report["bonds"] += len(res["bonds"])

        for ct in res["contacts"]:
            x, y = atom_ids.get(ct["a"]), atom_ids.get(ct["b"])
            if x is None or y is None:
                continue
            edge("CONTACT", x, y, {
                "kind": ct["kind"], "length": ct["length"],
                "angle": ct["angle"], "symop": ct["symop"],
                "symop_triplet": ct["triplet"], "h_inferred": ct["h_inferred"],
                "has_angle": ct["angle"] is not None,
                "cod_id": cod_id,
            })
            report[f"contact_{ct['kind']}"] += 1
            if ct["h_inferred"]:
                report["contact_hbond_inferred"] += 1
        report["contacts"] += len(res["contacts"])

        # ---- Components + Fragments -------------------------------------
        for comp in res["components"]:
            key = comp["inchikey"] or comp["fallback_key"] or f"poly_{comp['formula']}"
            props = {
                "component_key": key,
                "formula": comp["formula"],
                "n_atoms": comp["n_atoms"],
                "n_heavy": comp["n_heavy"],
                "charge": comp["charge"],
                "has_charge": comp["charge"] is not None,
                "is_solvent": comp["is_solvent"],
                "is_polymeric": comp["is_polymeric"],
                "has_inchikey": comp["inchikey"] is not None,
                "name": comp.get("name") or None,
            }
            if comp["inchikey"]:
                props["inchikey"] = comp["inchikey"]
                report["components_with_inchikey"] += 1
            else:
                props["fallback_key"] = comp["fallback_key"]
                if comp["is_polymeric"]:
                    report["components_polymeric"] += 1
                else:
                    report["components_fallback"] += 1
                    if comp["error"]:
                        perception_errors[comp["error"].split(":")[0]] += 1
            cid = reg.node("Component", key, props)
            report["components"] += 1
            edge("CONTAINS_COMPONENT", sid, cid, {
                "role": comp["role"], "n_sites": len(comp["sites"]),
            })
            for frag in comp["fragments"]:
                pattern = FRAGMENTS_BY_NAME.get(frag)
                fid = reg.node("Fragment", frag, {
                    "fragment_type": frag,               # `type` is reserved
                    "smarts": pattern.smarts if pattern else "",
                })
                if not _edge_exists(edges, cid, fid, "HAS_FRAGMENT"):
                    edge("HAS_FRAGMENT", cid, fid)
                report["fragment_links"] += 1

        # ---- Publication / Journal / Author ------------------------------
        doi = (meta.get("doi") or "").strip()
        pub_key = doi or f"cod:{cod_id}"
        if meta:
            pid = reg.node("Publication", pub_key, {
                "doi": doi or None,
                "year": year,
                "title": (meta.get("title") or "").strip() or None,
                # `volume` would collide with Structure.cell_volume: property
                # types are global in TuringDB
                "journal_volume": (meta.get("volume") or "").strip() or None,
                "pages": (meta.get("firstpage") or "").strip() or None,
                "has_doi": bool(doi),
            })
            edge("PUBLISHED_IN", sid, pid)

            jkey = meta.get("_journal_key")
            if jkey and jkey in JOURNALS_BY_KEY:
                jid = reg.node("Journal", jkey, {
                    "name": JOURNALS_BY_KEY[jkey].display,
                    "journal_key": jkey,
                })
                if not _edge_exists(edges, pid, jid, "IN_JOURNAL"):
                    edge("IN_JOURNAL", pid, jid)

            authors = [x.strip() for x in (meta.get("authors") or "").split(";")]
            for pos, name in enumerate([a for a in authors if a][:12], start=1):
                nkey = _author_key(name)
                aid2 = reg.node("Author", nkey, {"name": name, "name_key": nkey})
                edge("AUTHORED_BY", pid, aid2, {"author_position": pos})

    # ---- SUPERSEDES from COD's own duplicate/optimal columns -------------
    n_supersedes = 0
    for cod_id, meta in metadata.items():
        dup = _int(meta.get("duplicateof"))
        if not dup:
            continue
        newer = reg.has("Structure", str(dup))
        older = reg.has("Structure", str(cod_id))
        if newer is None or older is None:
            continue
        status = (meta.get("status") or "").strip()
        edge("SUPERSEDES", newer, older, {
            "reason": status or "duplicate",
            "revision_kind": (
                "retraction" if status == "retracted"
                else "correction" if status == "errors"
                else "redetermination"
            ),
            # COD asserted this link via data.duplicateof, so it is NOT inferred
            "inferred": False,
            "source": "COD data.duplicateof",
        })
        n_supersedes += 1
    report["supersedes"] = n_supersedes

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh:
        for node in reg.nodes:
            fh.write(json.dumps(node, separators=(",", ":")) + "\n")
        for e in edges:
            fh.write(json.dumps(e, separators=(",", ":")) + "\n")

    summary = {
        "nodes": len(reg.nodes),
        "edges": len(edges),
        "counts": dict(report),
        "missing_fields": dict(missing),
        "perception_errors": dict(perception_errors),
        "criteria": ch.CRITERIA_PROVENANCE,
        "output": str(out_path),
        "bytes": out_path.stat().st_size,
    }
    return summary


def _edge_exists(edges: list[dict[str, Any]], src: int, tgt: int, label: str) -> bool:
    # Cheap guard for the few edge types that are naturally deduplicated.
    # Linear scan is fine because it is only consulted for Fragment/Journal.
    s, t = str(src), str(tgt)
    for e in reversed(edges[-400:]):
        if e["label"] == label and e["start"]["id"] == s and e["end"]["id"] == t:
            return True
    return False


def _author_key(name: str) -> str:
    return "".join(ch2.lower() for ch2 in name if ch2.isalnum())


_ATOMIC: dict[str, int] = {}


def _atomic_number(symbol: str) -> int | None:
    if not _ATOMIC:
        import gemmi

        for z in range(1, 119):
            try:
                _ATOMIC[gemmi.Element(z).name] = z
            except Exception:  # noqa: BLE001
                pass
    return _ATOMIC.get(symbol)


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default="slice")
    ap.add_argument("--data", default="data", type=Path)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--seed", type=int, default=20260903)
    # Single-threaded only. Both fork and spawn pools were tried and both hung:
    # forked workers inherit non-fork-safe gemmi/RDKit/scipy state, and under
    # spawn a worker died leaving imap_unordered blocked forever. The parse is
    # checkpointed instead, which recovers the robustness a pool was meant to buy.
    ap.add_argument("--workers", type=int, default=1,
                    help="accepted for compatibility; ingest is single-threaded")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-resume", action="store_true",
                    help="ignore any existing parse cache")
    ap.add_argument("--from-cache", action="store_true",
                    help="assemble the graph from the cache without parsing more")
    args = ap.parse_args(argv)

    man_dir = args.data / "manifest"
    meta_path = man_dir / f"{args.dataset}_metadata.csv"
    if not meta_path.exists():
        print(f"missing {meta_path}; run ingest.download first", file=sys.stderr)
        return 1

    metadata: dict[int, dict[str, str]] = {}
    with meta_path.open() as fh:
        for row in csv.DictReader(fh):
            try:
                metadata[int(row["file"])] = row
            except (KeyError, ValueError):
                continue
    print(f"metadata rows: {len(metadata):,}")

    cif_dir = args.data / "cif"
    tasks: list[tuple[str, int]] = []
    for cod_id in sorted(metadata):
        p = cif_dir / cod_cif_relpath(cod_id)
        if p.exists():
            tasks.append((str(p), cod_id))
    print(f"CIFs on disk: {len(tasks):,}")

    if args.limit and args.limit < len(tasks):
        import random

        tasks = random.Random(args.seed).sample(tasks, args.limit)
        tasks.sort(key=lambda t: t[1])
        print(f"--limit {args.limit}: sampled {len(tasks):,} (seed={args.seed})")

    out = Path(args.out) if args.out else args.data / "jsonl" / f"{args.dataset}.jsonl"

    # Per-structure results are streamed to a cache as they are produced, so a
    # crash mid-run loses nothing and a re-run resumes. Worth the disk: the
    # parse is the expensive half, and this ingest has died silently more than
    # once deep into a long run (no traceback, no OOM message), which is exactly
    # the failure mode a checkpoint exists for.
    cache = out.with_suffix(".cache.jsonl")
    cache.parent.mkdir(parents=True, exist_ok=True)
    done: dict[int, dict[str, Any]] = {}
    if cache.exists() and not args.no_resume:
        with cache.open() as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue          # truncated final line from a hard kill
                if isinstance(obj, dict) and "cod_id" in obj:
                    done[int(obj["cod_id"])] = obj
        if done:
            print(f"resuming: {len(done):,} structures already cached")
    # A crash-looping structure would otherwise be retried forever: it is not a
    # hang (the SIGALRM watchdog never fires) but a hard crash inside a C
    # extension, which kills the interpreter with no traceback. So we record the
    # ATTEMPT before parsing; any id that was attempted but produced no cached
    # result crashed the process and is skipped on the next run.
    attempts_path = out.with_suffix(".attempts.txt")
    attempted: set[int] = set()
    if attempts_path.exists() and not args.no_resume:
        for line in attempts_path.read_text().split():
            if line.isdigit():
                attempted.add(int(line))
    crashed = sorted(attempted - set(done))
    if crashed:
        print(f"skipping {len(crashed):,} structure(s) that crashed a previous "
              f"run: {crashed[:8]}{'...' if len(crashed) > 8 else ''}")
        for cid in crashed:
            done[cid] = {"cod_id": cid, "skipped": "crashed_parser"}

    todo = [t for t in tasks if t[1] not in done]
    print(f"to parse: {len(todo):,}")

    t0 = time.time()
    results: list[dict[str, Any]] = list(done.values())
    if todo and not args.from_cache:
        with cache.open("a") as ch_fh, attempts_path.open("a") as at_fh:
            for i, task in enumerate(todo, 1):
                at_fh.write(f"{task[1]}\n")
                at_fh.flush()          # must survive a hard crash
                res = process_structure(task)
                if res is not None:
                    results.append(res)
                    ch_fh.write(json.dumps(res, separators=(",", ":")) + "\n")
                    if i % 200 == 0:
                        ch_fh.flush()
                if i % 500 == 0:
                    rate = i / max(time.time() - t0, 1e-9)
                    print(f"  parsed {i:,}/{len(todo):,} ({rate:.0f}/s)", flush=True)
    parse_s = time.time() - t0
    print(f"parsed {len(results):,} in {parse_s:.0f}s "
          f"({len(results) / max(parse_s, 1e-9):.0f}/s)")

    summary = build(results, metadata, out)
    summary["parse_seconds"] = round(parse_s, 1)
    summary["dataset"] = args.dataset

    rep_path = man_dir / f"{args.dataset}_ingest_report.json"
    rep_path.write_text(json.dumps(summary, indent=2) + "\n")

    c = summary["counts"]
    print(f"\n=== ingest report ===")
    print(f"  structures      : {c.get('structures', 0):,}  "
          f"(skipped {c.get('skipped', 0):,})")
    print(f"  atoms           : {c.get('atoms', 0):,}")
    print(f"  bonds           : {c.get('bonds', 0):,}")
    print(f"  contacts        : {c.get('contacts', 0):,}  "
          f"(hbond {c.get('contact_hbond', 0):,} of which "
          f"{c.get('contact_hbond_inferred', 0):,} h_inferred, "
          f"halogen {c.get('contact_halogen', 0):,})")
    print(f"  components      : {c.get('components', 0):,}  "
          f"(InChIKey {c.get('components_with_inchikey', 0):,}, "
          f"fallback {c.get('components_fallback', 0):,}, "
          f"polymeric {c.get('components_polymeric', 0):,})")
    print(f"  SUPERSEDES      : {c.get('supersedes', 0):,}")
    print(f"  nodes / edges   : {summary['nodes']:,} / {summary['edges']:,}")
    print(f"  missing fields  : {summary['missing_fields']}")
    print(f"  output          : {out}  ({summary['bytes'] / 1e6:.1f} MB)")
    print(f"  report          : {rep_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
