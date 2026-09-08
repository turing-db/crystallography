"""Inspect gemmi's small-structure + symmetry API against a real COD CIF.

Symmetry expansion is where this pipeline is most likely to be silently wrong,
so before writing any of it we check what gemmi actually offers on the installed
version rather than assuming an API shape.
"""

from __future__ import annotations

import glob
import sys

import gemmi


def members(obj: object, needle: str = "") -> list[str]:
    return sorted(
        m for m in dir(obj)
        if not m.startswith("_") and (needle.lower() in m.lower() if needle else True)
    )


def main() -> int:
    print(f"gemmi {gemmi.__version__}\n")

    paths = sorted(glob.glob("data/cif/**/*.cif", recursive=True))
    if not paths:
        print("no CIFs found under data/cif -- run ingest.download first")
        return 1
    print(f"{len(paths)} CIFs available; using {paths[0]}\n")

    print("=== module-level readers ===")
    print(members(gemmi, "small"))
    print(members(gemmi, "read"))

    st = gemmi.read_small_structure(paths[0])
    print(f"\n=== SmallStructure ({type(st).__name__}) ===")
    print(members(st))

    print(f"\n  name          = {st.name}")
    print(f"  spacegroup_hm = {st.spacegroup_hm!r}")
    print(f"  cell          = {st.cell}")
    print(f"  n sites       = {len(st.sites)}")

    print(f"  spacegroup_hall = {st.spacegroup_hall!r}")
    print(f"  spacegroup_number = {st.spacegroup_number}")
    print(f"  symops ({len(st.symops)}): {[str(o) for o in st.symops][:4]}")

    sg = st.spacegroup
    print(f"\n=== .spacegroup -> {sg} ===")
    if sg is not None:
        print(members(sg))
        print(f"  number={sg.number} hm={sg.hm!r} hall={sg.hall!r} "
              f"xhm={sg.xhm()!r} crystal_system={sg.crystal_system_str()!r}")
        ops = sg.operations()
        oplist = list(ops)
        print(f"  n operations = {len(oplist)}")
        for i, op in enumerate(oplist[:4]):
            print(f"    op[{i}] = {op.triplet()}")
    print(f"\n  check_spacegroup: {st.check_spacegroup}")
    print(f"  determine_and_set_spacegroup: {st.determine_and_set_spacegroup}")

    site = st.sites[0]
    print(f"\n=== SmallStructure.Site ===")
    print(members(site))
    print(f"  label={site.label!r} type_symbol={site.type_symbol!r} "
          f"element={site.element} occ={site.occ} u_iso={site.u_iso}")
    print(f"  fract={site.fract}  orth={site.orth(st.cell)}")

    print("\n=== unit-cell expansion helpers ===")
    for name in ("get_all_unit_cell_sites", "setup_cell_images", "cell"):
        print(f"  has {name}: {hasattr(st, name)}")
    if hasattr(st, "get_all_unit_cell_sites"):
        allsites = st.get_all_unit_cell_sites()
        print(f"  get_all_unit_cell_sites() -> {len(allsites)} sites "
              f"(asymmetric unit has {len(st.sites)})")
        print(f"  site type: {type(allsites[0]).__name__}, members: {members(allsites[0])}")

    print("\n=== NeighborSearch / ContactSearch ===")
    print("  NeighborSearch:", members(gemmi.NeighborSearch))
    print("  ContactSearch :", members(gemmi.ContactSearch))

    print("\n=== UnitCell ===")
    print(members(st.cell, "find"))
    print(members(st.cell, "image"))
    print(members(st.cell, "orth"))
    print(members(st.cell, "fract"))
    print(f"  volume = {st.cell.volume}")

    print("\n=== Element ===")
    el = gemmi.Element("O")
    print(members(el))
    print(f"  O: atomic_number={el.atomic_number} vdw_r={el.vdw_r} "
          f"covalent_r={el.covalent_r} weight={el.weight} is_metal={el.is_metal}")

    print("\n=== small structure -> Structure conversion (for NeighborSearch) ===")
    for name in ("make_structure_from_small", "small_to_structure"):
        print(f"  gemmi.{name}: {hasattr(gemmi, name)}")
    print(members(gemmi, "structure"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
