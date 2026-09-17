"""Re-derive every number the README and the UI quote, from the live graph.

Written because a review found that three of the five headline figures in the
README were computed over the wrong population, and nothing in the repo would
have caught it:

* "mean H...A 2.09 A at 160.5 deg" filtered on `has_angle`, which **includes the
  halogen bonds** -- so it averaged H...A together with X...A. The located-H
  hydrogen-bond value is 2.053 A / 160.30 deg.
* "centrosymmetric groups account for 74.0%" was the sum of five hand-picked
  space groups. Asked of the data it is 82.4%.
* "33,930 intermolecular contacts" blends three populations that the demo's own
  rules say must never be blended.

The fix is not to correct the prose once -- it is to make the prose quote a
script, so the next rebuild cannot drift from it. Run this after every rebuild
and paste the block it prints.

Usage:
    .venv/bin/python tests/corpus_facts.py --graph cod_slice_v2
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def scalar(host: str, graph: str, cypher: str) -> float:
    req = urllib.request.Request(
        f"{host}/query?graph={graph}",
        data=cypher.encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        payload = json.loads(r.read())
    if payload.get("error"):
        raise RuntimeError(f"{payload['error']}: {payload.get('error_details')}")
    data = payload.get("data") or [[[None]]]
    try:
        v = data[0][0][0]
    except (IndexError, TypeError):
        return 0.0
    return float(v) if v is not None else 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="http://localhost:6691")
    ap.add_argument("--graph", default="cod_slice_v2")
    ap.add_argument("--out", default="data/manifest/corpus_facts.json")
    a = ap.parse_args(argv)
    S = lambda c: scalar(a.host, a.graph, c)  # noqa: E731

    f: dict[str, float] = {}
    f["structures"] = S("MATCH (s:Structure) RETURN count(s)")
    f["atoms"] = S("MATCH (n:Atom) RETURN count(n)")
    f["components"] = S("MATCH (n:Component) RETURN count(n)")
    f["bonds"] = S("MATCH ()-[r:BONDED_TO]->() RETURN count(r)")
    f["space_groups"] = S("MATCH (n:SpaceGroup) RETURN count(n)")

    # --- the three contact populations, never blended ---------------------
    f["hbond_strong"] = S(
        "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' "
        "AND r.h_inferred = false RETURN count(r)")
    f["hbond_inferred"] = S(
        "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' "
        "AND r.h_inferred = true RETURN count(r)")
    f["hbond_weak"] = S(
        "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond_weak' RETURN count(r)")
    f["halogen"] = S(
        "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'halogen' RETURN count(r)")
    f["close_contact"] = S(
        "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'close_contact' RETURN count(r)")
    f["contacts_all"] = S("MATCH ()-[r:CONTACT]->() RETURN count(r)")

    # --- geometry, over the located-H HYDROGEN-BOND population only -------
    base = ("MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' "
            "AND r.h_inferred = false RETURN ")
    f["mean_HA"] = S(base + "avg(r.length)")
    f["mean_angle"] = S(base + "avg(r.angle)")
    wbase = "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond_weak' RETURN "
    f["mean_HA_weak"] = S(wbase + "avg(r.length)")
    f["mean_angle_weak"] = S(wbase + "avg(r.angle)")

    # --- symmetry, asked of the data rather than a hand list --------------
    f["centrosymmetric"] = S(
        "MATCH (s:Structure)-[:IN_SPACE_GROUP]->(x:SpaceGroup) "
        "WHERE x.is_centrosymmetric = true RETURN count(s)")

    # --- periodic-net dimensionality --------------------------------------
    for k in (0, 1, 2, 3):
        f[f"net_dim_{k}"] = S(
            f"MATCH (s:Structure) WHERE s.net_dim = {k} RETURN count(s)")
        f[f"net_dim_weak_{k}"] = S(
            f"MATCH (s:Structure) WHERE s.net_dim_weak = {k} RETURN count(s)")
    f["net_scored"] = sum(f[f"net_dim_{k}"] for k in range(4))
    f["net_scored_weak"] = sum(f[f"net_dim_weak_{k}"] for k in range(4))

    f["with_inchikey"] = S(
        "MATCH (c:Component) WHERE c.has_inchikey = true RETURN count(c)")
    f["acid_components"] = S(
        "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) "
        "WHERE f.fragment_type = 'carboxylic_acid' RETURN count(c)")

    pct = lambda x, y: 100.0 * x / y if y else 0.0  # noqa: E731
    print(f"\n=== {a.graph} ===\n")
    print(f"Structures            {f['structures']:>12,.0f}")
    print(f"Atomic sites          {f['atoms']:>12,.0f}")
    print(f"Components            {f['components']:>12,.0f}   "
          f"InChIKey {pct(f['with_inchikey'], f['components']):.1f}%")
    print(f"Covalent bonds        {f['bonds']:>12,.0f}")
    print(f"Space groups          {f['space_groups']:>12,.0f}   "
          f"centrosymmetric {pct(f['centrosymmetric'], f['structures']):.1f}% "
          f"of structures")
    print("\nContacts -- three populations, never blended:")
    print(f"  strong H-bond (located H)  {f['hbond_strong']:>12,.0f}   "
          f"mean H...A {f['mean_HA']:.3f} A at {f['mean_angle']:.2f} deg")
    print(f"  inferred (no located H)    {f['hbond_inferred']:>12,.0f}   "
          f"{pct(f['hbond_inferred'], f['hbond_strong'] + f['hbond_inferred']):.1f}% "
          f"of the H-bond layer")
    print(f"  weak C-H...A               {f['hbond_weak']:>12,.0f}   "
          f"mean H...A {f['mean_HA_weak']:.3f} A at {f['mean_angle_weak']:.2f} deg")
    print(f"  halogen bond               {f['halogen']:>12,.0f}")
    print(f"  close_contact (excluded)   {f['close_contact']:>12,.0f}")
    print(f"  ALL CONTACT edges          {f['contacts_all']:>12,.0f}")
    print("\nPeriodic-net dimensionality (h_inferred excluded):")
    lab = {0: "0D finite motif", 1: "1D chain", 2: "2D sheet", 3: "3D framework"}
    for k in (0, 1, 2, 3):
        print(f"  {lab[k]:<18} strong {f[f'net_dim_{k}']:>8,.0f} "
              f"({pct(f[f'net_dim_{k}'], f['net_scored']):>5.1f}%)"
              f"   +weak {f[f'net_dim_weak_{k}']:>8,.0f} "
              f"({pct(f[f'net_dim_weak_{k}'], f['net_scored_weak']):>5.1f}%)")
    print(f"  scored: {f['net_scored']:,.0f} strong / "
          f"{f['net_scored_weak']:,.0f} with weak\n")

    from pathlib import Path

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(f, indent=2) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
