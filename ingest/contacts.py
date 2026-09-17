"""Intermolecular contact detection: the edges nobody persists.

This is the layer the demo exists to argue about. Hydrogen bonds, halogen bonds
and pi-stacking are not stored in COD or the CSD -- they are recomputed
geometrically every time somebody asks a question. Persisting them as graph
edges is what turns "recompute the packing for every structure and then filter"
into a pattern match.

What counts as intermolecular
-----------------------------
Not simply "different components". Two symmetry-related copies of the SAME
component are different *molecules* in the crystal, and the contact between them
is usually the interesting one -- a carboxylic acid dimer across an inversion
centre is exactly that. So the test is whether the neighbouring atom belongs to
the same *molecule instance*, which means comparing (site, accumulated symmetry
operation) against the molecule containing the reference atom.

Criteria are defined and cited in ingest/chemistry.py; nothing is invented here.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import gemmi
import numpy as np

from . import chemistry as ch
from .periodic import PeriodicNeighbours
from .structure import Atom, Bond, _op_for
from .symmetry import SymOp

#: Bounds the per-component molecule walk. A component larger than this is an
#: extended framework, which structure.find_components already flags.
MEMBERSHIP_LIMIT = 600


@dataclass(frozen=True)
class Contact:
    """One intermolecular contact edge."""

    a_index: int
    b_index: int
    a_uid: str
    b_uid: str
    kind: str                 # 'hbond' | 'hbond_weak' | 'halogen' | 'pi_stack'
    length: float             # H...A, X...A, or centroid...centroid
    angle: float | None       # D-H...A, C-X...A, or interplanar
    symop: str
    symop_triplet: str
    h_inferred: bool
    #: for hbonds, the donor heavy atom and the H actually used (if any)
    donor_uid: str = ""
    hydrogen_uid: str = ""


def build_adjacency(
    sg: gemmi.SpaceGroup, bonds: list[Bond]
) -> dict[int, list[tuple[int, gemmi.Op]]]:
    """site -> [(neighbour site, op mapping neighbour's asym position next to it)]."""
    adj: dict[int, list[tuple[int, gemmi.Op]]] = {}
    for bond in bonds:
        try:
            op = _op_for(sg, bond.symop)
            inv = op.inverse()
        except Exception:  # noqa: BLE001
            continue
        adj.setdefault(bond.a_index, []).append((bond.b_index, op))
        adj.setdefault(bond.b_index, []).append((bond.a_index, inv))
    return adj


def molecule_membership(
    adj: dict[int, list[tuple[int, gemmi.Op]]],
    seed: int,
    limit: int = 2000,
) -> set[tuple[int, str]]:
    """(site, op-triplet) pairs forming the molecule that contains `seed` at identity."""
    from collections import deque

    identity = gemmi.Op("x,y,z")
    seen: set[tuple[int, str]] = {(seed, identity.triplet())}
    queue: deque[tuple[int, gemmi.Op]] = deque([(seed, identity)])
    while queue and len(seen) < limit:
        idx, acc = queue.popleft()
        for nbr, op in adj.get(idx, ()):
            new_acc = acc * op
            key = (nbr, new_acc.triplet())
            if key in seen:
                continue
            seen.add(key)
            queue.append((nbr, new_acc))
    return seen


def _angle_deg(a: np.ndarray, vertex: np.ndarray, b: np.ndarray) -> float:
    """Angle a-vertex-b in degrees."""
    v1, v2 = a - vertex, b - vertex
    n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
    if n1 < 1e-9 or n2 < 1e-9:
        return 0.0
    cos = float(np.dot(v1, v2) / (n1 * n2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))


def detect_contacts(
    st: gemmi.SmallStructure,
    sg: gemmi.SpaceGroup,
    atoms: list[Atom],
    bonds: list[Bond],
    neighbours: PeriodicNeighbours,
    positions: np.ndarray,
    components: list[list[int]] | None = None,
    time_budget_s: float | None = None,
) -> tuple[list[Contact], dict[str, int]]:
    """Hydrogen bonds and halogen bonds between distinct molecule instances.

    The `h_inferred` fallback is applied at MOLECULE granularity, not per atom.
    An oxygen with no attached hydrogen is ambiguous on its own: it could be a
    hydroxyl whose H was never refined, or a carbonyl that genuinely has none.
    Treating every such atom as a donor double-counts every hydrogen bond -- the
    real N-H...O appears once properly, then again backwards as O...N "inferred".
    So the fallback fires only when the donor's own molecule contains no
    hydrogen at all, which is exactly the "this entry has no H positions" case
    the criterion exists for, and correctly still covers an unrefined water in a
    structure whose organic component does have hydrogens.

    Three populations come out of this function and they are kept apart by
    `kind`: 'hbond' (strong, located H), 'hbond' with h_inferred=True (strong,
    heavy-atom fallback), and 'hbond_weak' (C-H...A, located H only). A query
    filtering kind='hbond' sees exactly what it saw before weak contacts
    existed.

    pi-stacking is not produced here: it needs ring perception, and the spec's
    centroid criterion alone admits badly offset stacks that most crystal
    engineers would not call pi-stacking. Shipping a half-defined criterion
    would be worse than shipping none. See the Known limits section of README.

    `time_budget_s` bounds the worst case. A handful of COD entries have a large
    asymmetric unit combined with many donors, and the candidate loop becomes
    very expensive -- COD 4504526 (147 sites, P2(1)/c) runs for minutes. A
    SIGALRM watchdog does not help there because the interpreter is inside a
    long run of Python-level work rather than one blocking call, so we check the
    clock in the loop instead and report truncation rather than hanging the
    whole ingest on one entry.
    """
    stats: dict[str, int] = {
        "hbond": 0, "hbond_inferred": 0, "hbond_weak": 0, "halogen": 0,
        "intramolecular_skipped": 0,
    }
    out: list[Contact] = []
    adj = build_adjacency(sg, bonds)

    # Molecule membership is computed ONCE PER COMPONENT, not once per donor.
    # The per-donor version ran a bounded BFS (with a gemmi Op multiplication and
    # a triplet() string per step) for every donor atom, which on an extended
    # framework meant tens of thousands of operations per structure and dominated
    # the whole ingest.
    #
    # For a component we walk from its seed site at the identity, giving the set
    # M0 of (site, op) pairs in that molecule. To ask whether atom A (sitting at
    # the identity) shares a molecule with atom B under operation S, we find the
    # op O_A that places A within M0 and test whether (B, O_A * S) is in M0 --
    # i.e. we re-express the question in the seed's frame instead of rebuilding
    # the molecule in A's frame.
    comp_of: dict[int, int] = {}
    comp_membership: dict[int, set[tuple[int, str]]] = {}
    comp_polymeric: set[int] = set()
    comp_anchor: dict[int, dict[int, str]] = {}

    if components:
        for ci, sites in enumerate(components):
            for s_ in sites:
                comp_of[s_] = ci

    def membership_for(ci: int) -> set[tuple[int, str]]:
        mem = comp_membership.get(ci)
        if mem is None:
            seed = min(components[ci]) if components else 0
            mem = molecule_membership(adj, seed, limit=MEMBERSHIP_LIMIT)
            comp_membership[ci] = mem
            # first op seen for each site, which is enough to change frames
            anchor: dict[int, str] = {}
            for site_i, trip in mem:
                anchor.setdefault(site_i, trip)
            comp_anchor[ci] = anchor
            if len(mem) >= MEMBERSHIP_LIMIT:
                comp_polymeric.add(ci)
        return mem

    def same_molecule(a_site: int, b_site: int, op: gemmi.Op) -> bool:
        ca, cb = comp_of.get(a_site), comp_of.get(b_site)
        if ca is None or cb is None:
            return False
        if ca != cb:
            return False               # different components: intermolecular
        mem = membership_for(ca)
        if ca in comp_polymeric:
            # An extended framework has no finite molecule; a contact inside it
            # is part of the framework, not an intermolecular interaction.
            return True
        anchor = comp_anchor[ca].get(a_site)
        if anchor is None:
            return False
        return (b_site, (gemmi.Op(anchor) * op).triplet()) in mem

    by_index = {a.site_index: a for a in atoms}

    # Which molecule each site belongs to, and whether that molecule has any
    # hydrogen at all. Drives the h_inferred fallback (see docstring).
    site_component: dict[int, int] = {}
    component_has_h: dict[int, bool] = {}
    if components:
        for ci, sites in enumerate(components):
            has_h = any(
                by_index.get(s) is not None and by_index[s].element == "H" for s in sites
            )
            component_has_h[ci] = has_h
            for s in sites:
                site_component[s] = ci

    def donor_molecule_lacks_hydrogen(site: int) -> bool:
        ci = site_component.get(site)
        if ci is None:
            # no component information: fall back to the whole structure
            return not any(a.element == "H" for a in atoms)
        return not component_has_h.get(ci, False)

    # H atoms attached to each heavy site, with the op putting them beside it
    hydrogens: dict[int, list[tuple[int, gemmi.Op]]] = {}
    for site, nbrs in adj.items():
        hs = [(n, op) for n, op in nbrs if by_index.get(n) and by_index[n].element == "H"]
        if hs:
            hydrogens[site] = hs

    seen: set[tuple[int, int, str, str]] = set()
    search_r = max(ch.HBOND_HEAVY_MAX, ch.MAX_CONTACT_SEARCH_RADIUS)

    # A weak-bond donor is a carbon that actually carries a refined hydrogen.
    # Gating on `hydrogens` here rather than inside the loop is what keeps the
    # cost down: in an entry with no H positions at all (much of Acta E) the
    # carbon set collapses to empty and the weak pass costs nothing.
    donors = [
        a for a in atoms
        if a.occupancy >= ch.MIN_BONDING_OCCUPANCY
        and (
            ch.is_hbond_element(a.element)
            or ch.is_halogen_donor(a.element)
            or (ch.is_weak_hbond_donor(a.element) and a.site_index in hydrogens)
        )
    ]
    if not donors:
        return out, stats

    hits = neighbours.query(positions[[a.site_index for a in donors]], search_r)

    started = time.monotonic()
    checked = 0
    out_of_time = False
    for n_done, (donor, rows) in enumerate(zip(donors, hits)):
        if out_of_time:
            stats["donors_unprocessed"] = len(donors) - n_done
            break
        d_pos = positions[donor.site_index]
        for row in rows:
            # The budget has to be enforced HERE, not just between donors: a
            # single donor in a large asymmetric unit can carry a very long
            # candidate list, so a per-donor check does not bound the work.
            checked += 1
            if time_budget_s is not None and (checked & 255) == 0:
                if time.monotonic() - started > time_budget_s:
                    stats["timed_out"] = 1
                    out_of_time = True
                    break
            image = neighbours.image_at(row)
            acceptor = by_index.get(image.site_index)
            if acceptor is None or acceptor.occupancy < ch.MIN_BONDING_OCCUPANCY:
                continue
            if not ch.is_hbond_element(acceptor.element):
                continue
            try:
                op = _op_for(sg, image.symop().cif_code())
            except Exception:  # noqa: BLE001
                continue
            if same_molecule(donor.site_index, acceptor.site_index, op):
                stats["intramolecular_skipped"] += 1
                continue

            a_pos = neighbours.xyz[row]
            symop = image.symop()
            key = (donor.site_index, acceptor.site_index, symop.cif_code(), "")
            if key in seen:
                continue

            # ---- hydrogen bond -------------------------------------------
            if ch.is_hbond_element(donor.element):
                attached = hydrogens.get(donor.site_index, [])
                made = False
                for h_site, h_op in attached:
                    h_atom = by_index.get(h_site)
                    if h_atom is None:
                        continue
                    hf = h_op.apply_to_xyz([h_atom.fract_x, h_atom.fract_y, h_atom.fract_z])
                    hp = st.cell.orthogonalize(gemmi.Fractional(*hf))
                    h_pos = np.array([hp.x, hp.y, hp.z])
                    d_ha = float(np.linalg.norm(a_pos - h_pos))
                    if d_ha >= ch.HBOND_MAX_H_ACCEPTOR:
                        continue
                    angle = _angle_deg(d_pos, h_pos, a_pos)
                    if angle <= ch.HBOND_MIN_ANGLE:
                        continue
                    k2 = (donor.site_index, acceptor.site_index, symop.cif_code(),
                          h_atom.uid)
                    if k2 in seen:
                        continue
                    seen.add(k2)
                    out.append(Contact(
                        a_index=donor.site_index, b_index=acceptor.site_index,
                        a_uid=donor.uid, b_uid=acceptor.uid, kind="hbond",
                        length=round(d_ha, 4), angle=round(angle, 2),
                        symop=symop.cif_code(), symop_triplet=symop.full_triplet(),
                        h_inferred=False, donor_uid=donor.uid,
                        hydrogen_uid=h_atom.uid,
                    ))
                    stats["hbond"] += 1
                    made = True
                if not made and not attached and donor_molecule_lacks_hydrogen(
                    donor.site_index
                ):
                    # No hydrogen was located on this donor. Fall back to the
                    # heavy-atom D...A separation and mark the edge so the two
                    # populations are never silently mixed.
                    d_da = float(np.linalg.norm(a_pos - d_pos))
                    if d_da < ch.HBOND_HEAVY_MAX:
                        # An inferred hbond has no hydrogen to give it direction,
                        # so D...A and A...D are the same edge. Key it on the
                        # unordered site pair to store it once.
                        undirected = (
                            min(donor.site_index, acceptor.site_index),
                            max(donor.site_index, acceptor.site_index),
                            "inferred",
                            f"{d_da:.2f}",
                        )
                        if undirected in seen:
                            continue
                        seen.add(undirected)
                        seen.add(key)
                        out.append(Contact(
                            a_index=donor.site_index, b_index=acceptor.site_index,
                            a_uid=donor.uid, b_uid=acceptor.uid, kind="hbond",
                            length=round(d_da, 4), angle=None,
                            symop=symop.cif_code(),
                            symop_triplet=symop.full_triplet(),
                            h_inferred=True, donor_uid=donor.uid,
                        ))
                        stats["hbond"] += 1
                        stats["hbond_inferred"] += 1

            # ---- weak hydrogen bond, C-H...A -----------------------------
            # Same geometry as the strong class, a looser distance ceiling, and
            # a separate `kind` so the two never merge in a query. No inferred
            # variant: without a located H this is not a hydrogen bond, it is
            # two atoms near each other (see chemistry.WEAK_HBOND_DONORS).
            if ch.is_weak_hbond_donor(donor.element):
                for h_site, h_op in hydrogens.get(donor.site_index, ()):
                    h_atom = by_index.get(h_site)
                    if h_atom is None:
                        continue
                    hf = h_op.apply_to_xyz(
                        [h_atom.fract_x, h_atom.fract_y, h_atom.fract_z]
                    )
                    hp = st.cell.orthogonalize(gemmi.Fractional(*hf))
                    h_pos = np.array([hp.x, hp.y, hp.z])
                    d_ha = float(np.linalg.norm(a_pos - h_pos))
                    if d_ha >= ch.WEAK_HBOND_MAX_H_ACCEPTOR:
                        continue
                    angle = _angle_deg(d_pos, h_pos, a_pos)
                    if angle <= ch.WEAK_HBOND_MIN_ANGLE:
                        continue
                    kw = (donor.site_index, acceptor.site_index,
                          symop.cif_code(), f"w{h_atom.uid}")
                    if kw in seen:
                        continue
                    seen.add(kw)
                    out.append(Contact(
                        a_index=donor.site_index, b_index=acceptor.site_index,
                        a_uid=donor.uid, b_uid=acceptor.uid, kind="hbond_weak",
                        length=round(d_ha, 4), angle=round(angle, 2),
                        symop=symop.cif_code(),
                        symop_triplet=symop.full_triplet(), h_inferred=False,
                        donor_uid=donor.uid, hydrogen_uid=h_atom.uid,
                    ))
                    stats["hbond_weak"] += 1

            # ---- halogen bond --------------------------------------------
            if ch.is_halogen_donor(donor.element):
                cutoff = ch.vdw_sum(donor.element, acceptor.element)
                if cutoff is None:
                    continue
                d_xa = float(np.linalg.norm(a_pos - d_pos))
                if d_xa >= cutoff:
                    continue
                # the sigma hole lies along the C-X axis, so the angle is
                # measured at the halogen from its bonded carbon
                carbons = [
                    (n, op2) for n, op2 in adj.get(donor.site_index, ())
                    if by_index.get(n) and by_index[n].element == "C"
                ]
                for c_site, c_op in carbons:
                    c_atom = by_index[c_site]
                    cf = c_op.apply_to_xyz(
                        [c_atom.fract_x, c_atom.fract_y, c_atom.fract_z]
                    )
                    cp = st.cell.orthogonalize(gemmi.Fractional(*cf))
                    angle = _angle_deg(
                        np.array([cp.x, cp.y, cp.z]), d_pos, a_pos
                    )
                    if angle <= ch.HALOGEN_MIN_ANGLE:
                        continue
                    k3 = (donor.site_index, acceptor.site_index,
                          symop.cif_code(), "X")
                    if k3 in seen:
                        continue
                    seen.add(k3)
                    out.append(Contact(
                        a_index=donor.site_index, b_index=acceptor.site_index,
                        a_uid=donor.uid, b_uid=acceptor.uid, kind="halogen",
                        length=round(d_xa, 4), angle=round(angle, 2),
                        symop=symop.cif_code(),
                        symop_triplet=symop.full_triplet(), h_inferred=False,
                    ))
                    stats["halogen"] += 1
                    break

    return out, stats
