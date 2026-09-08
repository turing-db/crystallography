"""Verify periodic neighbour search and, crucially, symmetry-operation recovery.

The spec is explicit that `symop` -- the symmetry operation plus lattice
translation that generated the neighbouring atom's position -- is the technical
heart of the ingest, because it is what lets a query traverse the infinite
periodic contact network without materialising a supercell.

So before writing the contact detector we establish, against a real COD entry:
  * how to build a NeighborSearch over a SmallStructure
  * what find_site_neighbors returns
  * how to turn a Mark back into (symmetry op index, lattice translation)
  * that the recovered symop actually reproduces the neighbour's position
"""

from __future__ import annotations

import glob
import inspect
import sys

import gemmi


def sig(fn: object, name: str) -> None:
    try:
        print(f"  {name}{inspect.signature(fn)}")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        doc = (getattr(fn, "__doc__", "") or "").strip().splitlines()
        print(f"  {name}: {doc[0] if doc else '<no signature>'}")


def main() -> int:
    paths = sorted(glob.glob("data/cif/**/*.cif", recursive=True))
    if not paths:
        print("no CIFs; run ingest.download first")
        return 1

    # pick a structure with a decent number of sites and a non-trivial group
    chosen = None
    for p in paths[:400]:
        try:
            s = gemmi.read_small_structure(p)
        except Exception:  # noqa: BLE001
            continue
        if s.spacegroup is not None and 10 <= len(s.sites) <= 60 and s.spacegroup.number > 1:
            chosen = (p, s)
            break
    if chosen is None:
        print("no suitable structure found")
        return 1
    path, st = chosen
    st.setup_cell_images()
    print(f"using {path}")
    print(f"  {st.name}  {st.spacegroup_hm} (#{st.spacegroup.number})  "
          f"{len(st.sites)} sites  cell={st.cell}\n")

    print("=== constructor / method signatures ===")
    sig(gemmi.NeighborSearch.__init__, "NeighborSearch.__init__")
    for m in ("add_site", "populate", "find_site_neighbors", "find_atoms",
              "get_image_transformation", "find_neighbors"):
        sig(getattr(gemmi.NeighborSearch, m, None), f"NeighborSearch.{m}")
    print()
    sig(getattr(gemmi.UnitCell, "find_nearest_pbc_image", None),
        "UnitCell.find_nearest_pbc_image")
    sig(getattr(gemmi.UnitCell, "find_nearest_pbc_position", None),
        "UnitCell.find_nearest_pbc_position")

    print("\n=== build the search ===")
    try:
        ns = gemmi.NeighborSearch(st, 5.0)
        print("  gemmi.NeighborSearch(small_structure, max_radius) OK")
    except Exception as exc:  # noqa: BLE001
        print(f"  small-structure ctor failed: {exc}")
        return 1
    ns.populate()
    print(f"  populated. grid_cell={ns.grid_cell} radius_specified={ns.radius_specified}")

    print("\n=== Mark structure ===")
    print("  Mark members:", sorted(m for m in dir(gemmi.NeighborSearch.Mark)
                                    if not m.startswith("_")))

    print("\n=== find_site_neighbors on site 0 ===")
    site0 = st.sites[0]
    marks = ns.find_site_neighbors(site0, min_dist=0.1, max_dist=4.0)
    print(f"  site {site0.label} ({site0.type_symbol}) -> {len(marks)} neighbours")

    all_sites = st.get_all_unit_cell_sites()
    print(f"  unit cell has {len(all_sites)} sites (asym {len(st.sites)})")

    ops = list(st.spacegroup.operations())
    print(f"  spacegroup has {len(ops)} operations")

    print("\n=== recovering the symmetry operation per contact ===")
    pos0 = st.cell.orthogonalize(site0.fract)
    shown = 0
    for mark in marks:
        if shown >= 6:
            break
        # image_idx 0 is the identity; >0 indexes cell images built by
        # setup_cell_images(). to_site maps the mark back to a unit-cell site.
        img = getattr(mark, "image_idx", None)
        try:
            ref = mark.to_site(all_sites)
            ref_label = ref.label
            ref_el = ref.type_symbol
        except Exception as exc:  # noqa: BLE001
            ref_label = f"<to_site failed: {exc}>"
            ref_el = "?"
        # the actual position of this periodic image
        mpos = mark.pos
        d = pos0.dist(mpos)
        # what transformation took the reference site to this image?
        try:
            ft = ns.get_image_transformation(img) if img is not None else None
            tr = f"mat={[round(v,3) for row in ft.mat.tolist() for v in row]} vec={[round(v,4) for v in ft.vec.tolist()]}" if ft else "identity"
        except Exception as exc:  # noqa: BLE001
            tr = f"<failed: {exc}>"
        print(f"  {site0.label}...{ref_label:<6} ({ref_el:<2}) d={d:5.3f} "
              f"image_idx={img}\n        {tr}")
        shown += 1

    print("\n=== ContactSearch (the higher-level API) ===")
    for m in ("search_radius", "ignore", "twice", "min_occupancy",
              "special_pos_cutoff_sq", "setup_atomic_radii", "set_radius",
              "get_radius", "find_contacts"):
        sig(getattr(gemmi.ContactSearch, m, None), f"ContactSearch.{m}")
    print("  Ignore enum:", sorted(m for m in dir(gemmi.ContactSearch.Ignore)
                                   if not m.startswith("_")))
    print("  Result members:", sorted(m for m in dir(gemmi.ContactSearch.Result)
                                      if not m.startswith("_")))

    print("\n=== gemmi's built-in radii (to decide whether to ship our own) ===")
    for symbol in ("H", "C", "N", "O", "F", "S", "Cl", "Br", "I", "Cu", "Zn", "Fe"):
        el = gemmi.Element(symbol)
        print(f"  {symbol:<3} Z={el.atomic_number:<3} covalent_r={el.covalent_r:.3f} "
              f"vdw_r={el.vdw_r:.3f} is_metal={el.is_metal}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
