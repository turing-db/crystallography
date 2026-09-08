"""Symmetry bookkeeping: turning a periodic neighbour into a citable symop.

This is the part of the ingest the spec calls its technical heart, because
`symop` is what lets a query walk the infinite periodic contact network without
anyone materialising a supercell. If this is wrong, every contact edge is wrong
and CCDC will see it immediately.

The encoding follows the CIF convention for `_geom_bond_site_symmetry_2`:

    <op_index>_<555 + translation>

so `2_565` means "symmetry operation number 2 of the space group, then translate
by (0, +1, 0) unit cells". Operation indices are 1-based and follow the order of
`SpaceGroup.operations()`, exactly as CIF requires. `1_555` is the identity in
the reference cell.

Every symop produced here is checked by `verify_symop`, which re-applies the
stored operation to the reference atom and asserts it reproduces the neighbour's
observed position. `tests/test_symmetry.py` runs that check over real COD
entries with known packing motifs.
"""

from __future__ import annotations

from dataclasses import dataclass

import gemmi

#: CIF encodes a zero lattice translation as 555, i.e. digit 5 means offset 0.
_CIF_TRANSLATION_ORIGIN = 5


@dataclass(frozen=True)
class SymOp:
    """One symmetry operation plus an integer lattice translation."""

    #: 1-based index into SpaceGroup.operations(), CIF convention
    op_index: int
    #: integer unit-cell translation applied after the operation
    translation: tuple[int, int, int]
    #: the operation's triplet, e.g. "-x,y+1/2,-z+1/2"
    triplet: str

    @property
    def is_identity(self) -> bool:
        return self.op_index == 1 and self.translation == (0, 0, 0)

    def cif_code(self) -> str:
        """CIF-style symmetry code, e.g. `2_565`."""
        a, b, c = (t + _CIF_TRANSLATION_ORIGIN for t in self.translation)
        return f"{self.op_index}_{a}{b}{c}"

    def full_triplet(self) -> str:
        """Triplet with the lattice translation folded in, e.g. `-x,y+3/2,-z+1/2`.

        This is the human-readable form a crystallographer will want to see in
        the UI; `cif_code()` is the compact form for the edge property.
        """
        if self.translation == (0, 0, 0):
            return self.triplet
        parts = self.triplet.split(",")
        if len(parts) != 3:
            return self.triplet
        out = []
        for part, shift in zip(parts, self.translation):
            part = part.strip()
            if shift:
                out.append(f"{part}{shift:+d}")
            else:
                out.append(part)
        return ",".join(out)

    def describe(self) -> str:
        return f"{self.cif_code()} [{self.full_triplet()}]"


def _wrap_to_cell(v: float) -> tuple[float, int]:
    """Fold a fractional coordinate into [0,1) and report the cells removed."""
    import math

    shift = math.floor(v)
    return v - shift, shift


def identity_symop(spacegroup: gemmi.SpaceGroup) -> SymOp:
    ops = list(spacegroup.operations())
    return SymOp(1, (0, 0, 0), ops[0].triplet() if ops else "x,y,z")


_RESOLVE_OPS: dict[int, list[gemmi.Op]] = {}


def resolve_symop(
    spacegroup: gemmi.SpaceGroup,
    cell: gemmi.UnitCell,
    ref_fract: gemmi.Fractional,
    observed_fract: gemmi.Fractional,
    tolerance: float = 1e-3,
) -> SymOp | None:
    """Find which symmetry operation maps `ref_fract` onto `observed_fract`.

    Rather than trusting an opaque image index, we re-derive the operation by
    applying each of the space group's operations to the reference atom and
    testing whether the result differs from the observed position by an integer
    lattice vector. That integer vector *is* the translation part, and the fact
    that it comes out integral is itself a check that the operation is right.

    Returns None when no operation reproduces the position, which the caller
    counts in the ingest report instead of inventing a symop.
    """
    ops = _RESOLVE_OPS.get(spacegroup.number)
    if ops is None:
        ops = list(spacegroup.operations())
        _RESOLVE_OPS[spacegroup.number] = ops
    for idx, op in enumerate(ops, start=1):
        transformed = op.apply_to_xyz([ref_fract.x, ref_fract.y, ref_fract.z])
        deltas = (
            observed_fract.x - transformed[0],
            observed_fract.y - transformed[1],
            observed_fract.z - transformed[2],
        )
        rounded = tuple(round(d) for d in deltas)
        if all(abs(d - r) < tolerance for d, r in zip(deltas, rounded)):
            return SymOp(idx, (int(rounded[0]), int(rounded[1]), int(rounded[2])),
                         op.triplet())
    return None


def apply_symop(
    spacegroup: gemmi.SpaceGroup,
    symop: SymOp,
    fract: gemmi.Fractional,
) -> gemmi.Fractional:
    """Apply a stored SymOp to a fractional position."""
    ops = list(spacegroup.operations())
    op = ops[symop.op_index - 1]
    x, y, z = op.apply_to_xyz([fract.x, fract.y, fract.z])
    return gemmi.Fractional(
        x + symop.translation[0],
        y + symop.translation[1],
        z + symop.translation[2],
    )


def verify_symop(
    spacegroup: gemmi.SpaceGroup,
    cell: gemmi.UnitCell,
    ref_fract: gemmi.Fractional,
    observed_fract: gemmi.Fractional,
    symop: SymOp,
    tolerance: float = 1e-3,
) -> float:
    """Re-apply a symop and return the residual distance in angstrom.

    A correct symop reproduces the neighbour's position essentially exactly, so
    a residual above a fraction of an angstrom means the operation stored on the
    edge is wrong. Used by the unit tests and by the ingest's self-check.
    """
    regenerated = apply_symop(spacegroup, symop, ref_fract)
    delta = gemmi.Fractional(
        regenerated.x - observed_fract.x,
        regenerated.y - observed_fract.y,
        regenerated.z - observed_fract.z,
    )
    cart = cell.orthogonalize(delta)
    origin = cell.orthogonalize(gemmi.Fractional(0.0, 0.0, 0.0))
    return ((cart.x - origin.x) ** 2 + (cart.y - origin.y) ** 2
            + (cart.z - origin.z) ** 2) ** 0.5


def spacegroup_of(st: gemmi.SmallStructure) -> gemmi.SpaceGroup | None:
    """Best-effort space group for a COD entry.

    COD entries vary in how the symmetry is recorded: some give a valid H-M
    symbol, some give only the operation list, some are simply wrong. We try the
    parsed group first, then let gemmi derive one from the listed operations.
    Returns None when neither works, which the caller records as a skip.
    """
    if st.spacegroup is not None:
        return st.spacegroup
    try:
        st.determine_and_set_spacegroup(gemmi.CheckSymmetry.No)
    except Exception:  # noqa: BLE001
        return None
    return st.spacegroup
