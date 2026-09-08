"""Turn one COD CIF into atoms, covalent bonds and discrete molecular components.

The interesting part is component perception. A CIF gives you an *asymmetric
unit*, which is generally a fraction of a molecule. The discrete molecules and
ions only appear once you expand by symmetry and take connected components of
the covalent bond graph -- and doing that naively either misses molecules that
straddle a symmetry element, or runs away forever on a coordination polymer.

We work on the **quotient graph**: nodes are asymmetric-unit sites, and each
covalent bond carries the symmetry operation that generated the neighbour. A
molecule is then assembled by walking that graph from a seed atom while
accumulating the symmetry operations, so each distinct (site, accumulated
operation) pair is one real atom of the molecule. That handles molecules on
special positions correctly, and it makes polymers detectable rather than fatal:
if the walk keeps producing new instances past a cap, the component is an
extended framework and is flagged `is_polymeric` instead of being handed to
InChI generation that could never succeed.
"""

from __future__ import annotations

import hashlib
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Iterable

import gemmi
import numpy as np

from . import chemistry as ch
from .periodic import PeriodicNeighbours, asymmetric_positions
from .symmetry import SymOp, resolve_symop, spacegroup_of

#: Assembling a molecule stops here. Anything larger is an extended framework
#: (coordination polymer, MOF, hydrogen-bonded network in the covalent graph),
#: which has no finite molecular formula and no InChIKey by definition.
MAX_COMPONENT_ATOMS = 400


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Atom:
    """One asymmetric-unit site."""

    uid: str
    label: str
    element: str
    fract_x: float
    fract_y: float
    fract_z: float
    occupancy: float
    u_iso: float | None
    #: index into SmallStructure.sites, used internally
    site_index: int

    @property
    def is_hydrogen(self) -> bool:
        return self.element == "H"


@dataclass(frozen=True)
class Bond:
    """A covalent bond between two asymmetric-unit sites.

    `symop` is the operation applied to site `b_index` to bring it next to site
    `a_index` in the reference cell. `1_555` means the bond lies wholly inside
    the asymmetric unit as deposited.
    """

    a_index: int
    b_index: int
    a_uid: str
    b_uid: str
    length: float
    symop: str
    symop_triplet: str


@dataclass
class Component:
    """A discrete molecule or ion: one connected component of the bond graph."""

    local_index: int
    #: asymmetric-unit site indices that make up this component (deduplicated)
    site_indices: list[int]
    #: how many real atoms the assembled molecule has (>= len(site_indices)
    #: when the molecule sits on a special position)
    n_atoms: int
    formula: str
    n_heavy: int
    charge: int | None
    inchikey: str | None
    #: set when InChI perception failed; a formula+connectivity digest instead
    fallback_key: str | None
    is_polymeric: bool
    perception_error: str = ""
    fragments: list[str] = field(default_factory=list)
    #: 'principal' | 'solvent' | 'counter_ion' -- assigned in build_graph, where
    #: the whole structure is visible
    role: str = ""
    is_solvent: bool = False

    @property
    def key(self) -> str:
        """Stable cross-structure identity for this component."""
        return self.inchikey or self.fallback_key or f"unknown_{self.formula}"


@dataclass
class ParsedStructure:
    cod_id: int
    path: str
    spacegroup_hm: str | None
    spacegroup_number: int | None
    crystal_system: str | None
    cell_a: float
    cell_b: float
    cell_c: float
    cell_alpha: float
    cell_beta: float
    cell_gamma: float
    cell_volume: float
    atoms: list[Atom]
    bonds: list[Bond]
    components: list[Component]
    #: diagnostics for the ingest report
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def _clean_float(value: float | None) -> float | None:
    """Reject the sentinel values COD uses for 'not measured'."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def read_structure(path: str) -> tuple[gemmi.SmallStructure, gemmi.SpaceGroup] | None:
    """Read a COD CIF and resolve its space group, or None if unusable."""
    try:
        st = gemmi.read_small_structure(path)
    except Exception:  # noqa: BLE001
        return None
    if not st.sites:
        return None
    sg = spacegroup_of(st)
    if sg is None:
        return None
    try:
        st.setup_cell_images()
    except Exception:  # noqa: BLE001
        return None
    return st, sg


def build_atoms(st: gemmi.SmallStructure, cod_id: int) -> list[Atom]:
    """One Atom per asymmetric-unit site, keeping every site including partials."""
    atoms: list[Atom] = []
    seen_labels: Counter[str] = Counter()
    for i, site in enumerate(st.sites):
        element = ch.normalise_element(site.type_symbol or str(site.element))
        label = site.label or f"{element}{i}"
        # COD occasionally repeats a site label; disambiguate so uids stay unique
        seen_labels[label] += 1
        if seen_labels[label] > 1:
            label = f"{label}#{seen_labels[label]}"
        atoms.append(
            Atom(
                uid=f"{cod_id}_{label}",
                label=label,
                element=element,
                fract_x=site.fract.x,
                fract_y=site.fract.y,
                fract_z=site.fract.z,
                occupancy=float(site.occ) if site.occ else 1.0,
                u_iso=_clean_float(site.u_iso),
                site_index=i,
            )
        )
    return atoms


# --------------------------------------------------------------------------
# covalent bonds
# --------------------------------------------------------------------------


def detect_bonds(
    st: gemmi.SmallStructure,
    sg: gemmi.SpaceGroup,
    atoms: list[Atom],
    neighbours: PeriodicNeighbours | None = None,
) -> tuple[list[Bond], list[str]]:
    """Covalent bonds within the asymmetric unit and to symmetry neighbours.

    Criterion (see ingest/chemistry.py for the cited radii):
        d(A,B) < r_cov(A) + r_cov(B) + 0.40 A

    Sites with occupancy below 0.5 are excluded, so that alternative components
    of a disorder -- which routinely sit within bonding distance of each other --
    are not bonded together.

    Runs over explicitly generated symmetry images (see ingest/periodic.py), so
    each bond's `symop` is known by construction rather than inferred.
    """
    notes: list[str] = []
    bonds: list[Bond] = []
    seen: set[tuple[int, int, str]] = set()

    bondable = [a for a in atoms if a.occupancy >= ch.MIN_BONDING_OCCUPANCY]
    if not bondable:
        notes.append("no sites above the occupancy threshold")
        return bonds, notes

    max_cut = _max_bond_cutoff(atoms)
    fracts = np.array([[a.fract_x, a.fract_y, a.fract_z] for a in atoms])
    if neighbours is None:
        neighbours = PeriodicNeighbours(st, sg, fracts, max_cut)
    if neighbours.tree is None:
        notes.append("no symmetry images generated")
        return bonds, notes
    if neighbours.truncated:
        notes.append("symmetry expansion truncated (very large image count)")

    positions = asymmetric_positions(st.cell, fracts)
    missing_radius: set[str] = set()
    by_index = {a.site_index: a for a in atoms}

    hits = neighbours.query(positions[[a.site_index for a in bondable]], max_cut)
    for atom, rows in zip(bondable, hits):
        p0 = positions[atom.site_index]
        for row in rows:
            image = neighbours.image_at(row)
            other = by_index.get(image.site_index)
            if other is None or other.occupancy < ch.MIN_BONDING_OCCUPANCY:
                continue
            symop = image.symop()
            # skip the atom's own identity image
            if other.site_index == atom.site_index and symop.is_identity:
                continue
            cutoff = ch.bond_cutoff(atom.element, other.element)
            if cutoff is None:
                missing_radius.add(f"{atom.element}/{other.element}")
                continue
            d = float(np.linalg.norm(neighbours.xyz[row] - p0))
            if d >= cutoff or d < 0.4:
                continue
            # Each bond is discovered twice, once from each end: atom A at the
            # identity bonded to B under S is the same bond as B at the identity
            # bonded to A under S-inverse. Canonicalise onto the lower site index
            # so the graph gets one BONDED_TO edge, not two.
            canon = _canonical_bond(sg, atom, other, symop)
            if canon is None:
                continue
            lo, hi, lo_uid, hi_uid, canon_symop = canon
            key = (lo, hi, canon_symop.cif_code())
            if key in seen:
                continue
            seen.add(key)
            bonds.append(
                Bond(
                    a_index=lo,
                    b_index=hi,
                    a_uid=lo_uid,
                    b_uid=hi_uid,
                    length=round(d, 4),
                    symop=canon_symop.cif_code(),
                    symop_triplet=canon_symop.full_triplet(),
                )
            )
    if missing_radius:
        notes.append(
            "no covalent radius for element pairs: " + ", ".join(sorted(missing_radius))
        )
    return bonds, notes


def _invert_symop(sg: gemmi.SpaceGroup, symop: SymOp) -> SymOp | None:
    """The operation that undoes `symop`, expressed in CIF index+translation form."""
    try:
        op = _op_for(sg, symop.cif_code()).inverse()
    except Exception:  # noqa: BLE001
        return None
    den = gemmi.Op.DEN
    # split the inverse back into (rotation-with-fractional-translation, lattice shift)
    base = gemmi.Op(op.triplet())
    shifts: list[int] = []
    for k in range(3):
        # fold the translation into [0, 1) and record the whole cells removed
        t = base.tran[k]
        cells = t // den if t >= 0 else -((-t + den - 1) // den)
        shifts.append(int(cells))
    folded = gemmi.Op(base.triplet())
    folded.tran = tuple(
        base.tran[k] - shifts[k] * den for k in range(3)
    )  # type: ignore[assignment]
    for idx, cand in enumerate(sg.operations(), start=1):
        if cand.triplet() == folded.triplet():
            return SymOp(idx, (shifts[0], shifts[1], shifts[2]), cand.triplet())
    return None


def _canonical_bond(
    sg: gemmi.SpaceGroup,
    atom: Atom,
    other: Atom,
    symop: SymOp,
) -> tuple[int, int, str, str, SymOp] | None:
    """Orient a bond onto the lower site index so it is stored exactly once."""
    a, b = atom.site_index, other.site_index
    if a < b:
        return a, b, atom.uid, other.uid, symop
    if a > b:
        inv = _invert_symop(sg, symop)
        if inv is None:
            return None
        return b, a, other.uid, atom.uid, inv
    # a == b: a site bonded to its own symmetry image. Keep whichever of the
    # operation and its inverse sorts first, so the pair collapses to one edge.
    inv = _invert_symop(sg, symop)
    if inv is None:
        return None
    if symop.cif_code() <= inv.cif_code():
        return a, b, atom.uid, other.uid, symop
    return a, b, other.uid, atom.uid, inv


def _max_bond_cutoff(atoms: list[Atom]) -> float:
    """Widest bond cutoff needed for the elements actually present.

    Bounding by the elements in this structure rather than by the whole periodic
    table keeps the neighbour search tight: francium's 2.60 A radius would
    otherwise force a 5.6 A search on every organic molecule.
    """
    radii = [r for r in (ch.covalent_radius(a.element) for a in atoms) if r is not None]
    if not radii:
        return 2.0
    return 2 * max(radii) + ch.BOND_TOLERANCE


# --------------------------------------------------------------------------
# periodic connected components
# --------------------------------------------------------------------------


#: Memo for _op_for. Rebuilding a gemmi.Op parses a triplet string, and contact
#: detection asks for one per candidate neighbour -- hundreds of thousands of
#: times on a large structure, which dominated the ingest once the RDKit hang
#: was fixed. Keyed by space-group number so it is safe across structures.
_OP_CACHE: dict[tuple[int, str], gemmi.Op] = {}
_OPS_CACHE: dict[int, list[gemmi.Op]] = {}


def _operations(sg: gemmi.SpaceGroup) -> list[gemmi.Op]:
    """sg.operations() materialised once per space group."""
    got = _OPS_CACHE.get(sg.number)
    if got is None:
        got = list(sg.operations())
        _OPS_CACHE[sg.number] = got
    return got


def _op_for(sg: gemmi.SpaceGroup, symop_code: str) -> gemmi.Op:
    """Rebuild a gemmi.Op (including lattice translation) from a CIF symop code."""
    key = (sg.number, symop_code)
    got = _OP_CACHE.get(key)
    if got is not None:
        return got
    idx_s, tr_s = symop_code.split("_")
    base = _operations(sg)[int(idx_s) - 1]
    op = gemmi.Op(base.triplet())  # gemmi.Op has no clone(); round-trip the triplet
    den = gemmi.Op.DEN
    shifts = [int(c) - 5 for c in tr_s]
    op.tran = (
        op.tran[0] + shifts[0] * den,
        op.tran[1] + shifts[1] * den,
        op.tran[2] + shifts[2] * den,
    )
    _OP_CACHE[key] = op
    return op


def find_components(
    st: gemmi.SmallStructure,
    sg: gemmi.SpaceGroup,
    atoms: list[Atom],
    bonds: list[Bond],
) -> tuple[list[list[int]], set[int], list[str]]:
    """Connected components of the periodic covalent graph.

    Returns (components as lists of site indices, indices of polymeric
    components, notes). Walks the quotient graph accumulating symmetry
    operations so that molecules sitting on special positions are assembled
    correctly and extended frameworks are detected rather than looped over.
    """
    notes: list[str] = []
    adjacency: dict[int, list[tuple[int, gemmi.Op]]] = {a.site_index: [] for a in atoms}
    for bond in bonds:
        try:
            op = _op_for(sg, bond.symop)
            inv = op.inverse()
        except Exception:  # noqa: BLE001
            continue
        adjacency[bond.a_index].append((bond.b_index, op))
        adjacency[bond.b_index].append((bond.a_index, inv))

    unassigned = {a.site_index for a in atoms if a.occupancy >= ch.MIN_BONDING_OCCUPANCY}
    components: list[list[int]] = []
    polymeric: set[int] = set()

    while unassigned:
        seed = min(unassigned)
        identity = gemmi.Op("x,y,z")
        queue: deque[tuple[int, gemmi.Op]] = deque([(seed, identity)])
        visited: set[tuple[int, str]] = {(seed, identity.triplet())}
        sites_here: set[int] = {seed}
        overflowed = False

        while queue:
            idx, acc = queue.popleft()
            for nbr, op in adjacency.get(idx, ()):
                new_acc = acc * op
                key = (nbr, new_acc.triplet())
                if key in visited:
                    continue
                visited.add(key)
                sites_here.add(nbr)
                if len(visited) > MAX_COMPONENT_ATOMS:
                    overflowed = True
                    break
                queue.append((nbr, new_acc))
            if overflowed:
                break

        comp_index = len(components)
        components.append(sorted(sites_here))
        if overflowed:
            polymeric.add(comp_index)
        unassigned -= sites_here

    if polymeric:
        notes.append(f"{len(polymeric)} extended/polymeric component(s)")
    return components, polymeric, notes


def component_instances(
    sg: gemmi.SpaceGroup,
    atoms: list[Atom],
    bonds: list[Bond],
    site_indices: Iterable[int],
) -> list[tuple[int, gemmi.Op]]:
    """Enumerate the real atoms of one finite component as (site, operation)."""
    wanted = set(site_indices)
    adjacency: dict[int, list[tuple[int, gemmi.Op]]] = {i: [] for i in wanted}
    for bond in bonds:
        if bond.a_index not in wanted or bond.b_index not in wanted:
            continue
        try:
            op = _op_for(sg, bond.symop)
            inv = op.inverse()
        except Exception:  # noqa: BLE001
            continue
        adjacency[bond.a_index].append((bond.b_index, op))
        adjacency[bond.b_index].append((bond.a_index, inv))

    seed = min(wanted)
    identity = gemmi.Op("x,y,z")
    out: list[tuple[int, gemmi.Op]] = [(seed, identity)]
    seen = {(seed, identity.triplet())}
    queue: deque[tuple[int, gemmi.Op]] = deque([(seed, identity)])
    while queue and len(out) <= MAX_COMPONENT_ATOMS:
        idx, acc = queue.popleft()
        for nbr, op in adjacency.get(idx, ()):
            new_acc = acc * op
            key = (nbr, new_acc.triplet())
            if key in seen:
                continue
            seen.add(key)
            out.append((nbr, new_acc))
            queue.append((nbr, new_acc))
    return out


def hill_formula(counts: Counter[str]) -> str:
    """Chemical formula in Hill order: C, then H, then everything alphabetical."""
    parts: list[str] = []
    for el in ("C", "H"):
        if counts.get(el):
            parts.append(el if counts[el] == 1 else f"{el}{counts[el]}")
    for el in sorted(k for k in counts if k not in ("C", "H")):
        if counts[el]:
            parts.append(el if counts[el] == 1 else f"{el}{counts[el]}")
    return " ".join(parts)


def connectivity_digest(
    atoms: list[Atom],
    instances: list[tuple[int, gemmi.Op]],
    bonds: list[Bond],
) -> str:
    """Deterministic identity for a component when InChI perception fails.

    Formula plus a digest of the element-pair bond multiset. This is weaker than
    an InChIKey -- it cannot distinguish constitutional isomers -- so it is
    stored in a separate property (`fallback_key`) and never presented as an
    InChIKey. The spec asks for exactly this fallback, and the ingest report
    counts how often we land here.
    """
    site_set = {i for i, _ in instances}
    counts: Counter[str] = Counter()
    for i, _ in instances:
        counts[atoms[i].element] += 1
    pairs: Counter[str] = Counter()
    for bond in bonds:
        if bond.a_index in site_set and bond.b_index in site_set:
            a, b = sorted((atoms[bond.a_index].element, atoms[bond.b_index].element))
            pairs[f"{a}-{b}"] += 1
    payload = hill_formula(counts) + "|" + ";".join(
        f"{k}:{v}" for k, v in sorted(pairs.items())
    )
    return "CX" + hashlib.sha256(payload.encode()).hexdigest()[:22].upper()
