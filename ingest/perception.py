"""RDKit perception: InChIKey identity and SMARTS fragment matching.

Component identity is what lets the graph say "this is the same molecule as in
that other structure", which is the basis of the coformer and polymorph
questions. InChIKey is the right identifier when it can be obtained.

It often cannot. A CIF gives atoms, coordinates and (after our geometry) a
connectivity table -- but no bond orders, no formal charges and frequently no
hydrogens. RDKit can infer bond orders from geometry for ordinary organics, and
fails predictably on metal complexes, where the very notion of a covalent bond
order is not well defined. That failure rate is reported rather than hidden,
because it bounds how good the fragment-based queries can be.

Where perception fails we fall back to a formula + connectivity digest, stored
in a DIFFERENT property so it is never mistaken for an InChIKey.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import gemmi

from .fragments import FragmentPattern, compiled_patterns
from .rdkit_guard import BondOrderPerceiver
from .structure import Atom, Bond, hill_formula

#: Above this atom count we do not attempt bond-order perception. RDKit's
#: DetermineBondOrders searches over bond-order assignments and its cost grows
#: sharply with size; on large components it can run for minutes and was the
#: single thing making the ingest appear to hang.
MAX_PERCEPTION_ATOMS = 60

_PATTERNS: list[tuple[FragmentPattern, object]] | None = None

#: One killable RDKit worker per ingest process, created on first use.
_PERCEIVER: "BondOrderPerceiver | None" = None


def _perceiver() -> "BondOrderPerceiver":
    global _PERCEIVER
    if _PERCEIVER is None:
        from .rdkit_guard import BondOrderPerceiver

        _PERCEIVER = BondOrderPerceiver()
    return _PERCEIVER


def perception_timeouts() -> int:
    """How many components were abandoned to a killed worker."""
    return _PERCEIVER.timeouts if _PERCEIVER is not None else 0


def _has_metal(atoms: list[Atom], instances: list[tuple[int, gemmi.Op]]) -> bool:
    for si, _ in instances:
        try:
            if gemmi.Element(atoms[si].element).is_metal:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _patterns() -> list[tuple[FragmentPattern, object]]:
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = compiled_patterns()
    return _PATTERNS


@dataclass
class Perceived:
    inchikey: str | None
    fallback_key: str | None
    formula: str
    n_atoms: int
    n_heavy: int
    charge: int | None
    fragments: list[str]
    #: fragment type -> the SITE INDICES it matched, so a query can ask which
    #: atom belongs to the group rather than only which molecule contains one
    fragment_sites: dict[str, list[int]] = field(default_factory=dict)
    error: str = ""


def perceive_component(
    cell: gemmi.UnitCell,
    atoms: list[Atom],
    bonds: list[Bond],
    instances: list[tuple[int, gemmi.Op]],
    sg: gemmi.SpaceGroup,
) -> Perceived:
    """Build an RDKit molecule for one component and derive its identity."""
    from rdkit import Chem, RDLogger
    from rdkit.Geometry import Point3D

    RDLogger.DisableLog("rdApp.*")

    counts: Counter[str] = Counter()
    for si, _ in instances:
        counts[atoms[si].element] += 1
    formula = hill_formula(counts)
    n_atoms = len(instances)
    n_heavy = sum(v for k, v in counts.items() if k != "H")

    # index the instance list so bonds can be mapped onto molecule atoms
    slot: dict[tuple[int, str], int] = {
        (si, op.triplet()): i for i, (si, op) in enumerate(instances)
    }

    mol = Chem.RWMol()
    conf_pos: list[tuple[float, float, float]] = []
    for si, op in instances:
        a = atoms[si]
        idx = mol.AddAtom(Chem.Atom(a.element or "C"))
        assert idx == len(conf_pos)
        fx, fy, fz = op.apply_to_xyz([a.fract_x, a.fract_y, a.fract_z])
        p = cell.orthogonalize(gemmi.Fractional(fx, fy, fz))
        conf_pos.append((p.x, p.y, p.z))

    # add each bond exactly once, in the molecule's own frame
    added: set[tuple[int, int]] = set()
    from .contacts import build_adjacency

    adj = build_adjacency(sg, bonds)
    for (si, op) in instances:
        i = slot[(si, op.triplet())]
        for nbr, bop in adj.get(si, ()):
            j = slot.get((nbr, (op * bop).triplet()))
            if j is None or i == j:
                continue
            key = (min(i, j), max(i, j))
            if key in added:
                continue
            added.add(key)
            mol.AddBond(i, j, Chem.BondType.SINGLE)

    conf = Chem.Conformer(mol.GetNumAtoms())
    for i, (x, y, z) in enumerate(conf_pos):
        conf.SetAtomPosition(i, Point3D(x, y, z))
    mol.AddConformer(conf)

    fallback = _digest(counts, atoms, instances, adj, slot)

    # Do not even attempt bond-order perception where it is known to fail or to
    # cost minutes. Metal complexes have no well-defined covalent bond order, so
    # InChI could not represent them anyway; skipping is both faster and more
    # honest than letting a combinatorial search grind and then fail.
    skip_reason = ""
    if n_atoms > MAX_PERCEPTION_ATOMS:
        skip_reason = f"component too large ({n_atoms} atoms)"
    elif _has_metal(atoms, instances):
        skip_reason = "metal-containing component"
    if skip_reason:
        # Do NOT attempt fragment matching either when the component is over the
        # size cap. _safe_fragments sanitises with SANITIZE_SYMMRINGS, and
        # RDKit's symmetrised ring perception is what actually hangs on these
        # graphs -- COD 4504526 still never returned after the bond-order search
        # was already being skipped. Metal components are small enough to match.
        frags = [] if n_atoms > MAX_PERCEPTION_ATOMS else _safe_fragments(mol)
        return Perceived(
            None, fallback, formula, n_atoms, n_heavy, None, frags,
            error="skipped: " + skip_reason,
        )

    try:
        m = mol.GetMol()
        # Infer bond orders and formal charges from the 3D geometry. This is the
        # step that fails on metal complexes -- and, on a small number of
        # ordinary organics, the step that NEVER RETURNS. It is a C++ call, so
        # the ingest's own SIGALRM cannot interrupt it; it runs in a child
        # process the parent can kill instead. See ingest/rdkit_guard.py.
        status, payload = _perceiver().perceive(Chem.MolToMolBlock(m, kekulize=False))
        if status != "ok":
            raise ValueError(str(payload))
        key = payload["inchikey"]
        charge = payload["charge"]
        # Fragment matching must run on the PERCEIVED molecule, not on `m`.
        # Bond-order perception happens in the child process, so `m` still
        # carries none and every bond-order-sensitive SMARTS would fail to
        # match -- silently, since an unmatched pattern is indistinguishable
        # from an absent group. The perceived structure comes back as SMILES:
        # atom order is irrelevant to a SMARTS census, connectivity and bond
        # order are not.
        # The child matched the SMARTS on the perceived molecule, so these
        # indices are in the caller's own atom order and map straight back to
        # crystallographic sites through `instances`.
        frags = payload.get("fragments") or []
        frag_sites = {
            name: sorted({instances[i][0] for i in idxs if i < len(instances)})
            for name, idxs in (payload.get("fragment_atoms") or {}).items()
        }
        return Perceived(key, fallback, formula, n_atoms, n_heavy, charge, frags,
                         fragment_sites=frag_sites)
    except Exception as exc:  # noqa: BLE001
        # Still try fragment matching on the unsanitised connectivity: many
        # SMARTS here are purely topological and match without bond orders.
        frags = _safe_fragments(mol)
        return Perceived(
            None, fallback, formula, n_atoms, n_heavy, None, frags,
            error=type(exc).__name__ + ": " + str(exc)[:120],
        )


def _safe_fragments(rwmol: object) -> list[str]:
    """Fragment matching on connectivity alone, tolerating an unsanitised mol."""
    from rdkit import Chem

    try:
        m = rwmol.GetMol()  # type: ignore[attr-defined]
        Chem.SanitizeMol(
            m,
            sanitizeOps=Chem.SanitizeFlags.SANITIZE_SYMMRINGS
            | Chem.SanitizeFlags.SANITIZE_SETCONJUGATION,
        )
        return _match_fragments(m)
    except Exception:  # noqa: BLE001
        return []


def _match_fragments(mol: object) -> list[str]:
    out: list[str] = []
    for pattern, query in _patterns():
        try:
            if mol.HasSubstructMatch(query):  # type: ignore[attr-defined]
                out.append(pattern.name)
        except Exception:  # noqa: BLE001
            continue
    return out


def _digest(
    counts: Counter[str],
    atoms: list[Atom],
    instances: list[tuple[int, gemmi.Op]],
    adj: dict[int, list[tuple[int, object]]],
    slot: dict[tuple[int, str], int],
) -> str:
    """Formula + bonded-element-pair multiset digest. Weaker than an InChIKey."""
    import hashlib

    pairs: Counter[str] = Counter()
    for (si, op) in instances:
        for nbr, bop in adj.get(si, ()):
            if (nbr, (op * bop).triplet()) in slot:  # type: ignore[operator]
                a, b = sorted((atoms[si].element, atoms[nbr].element))
                pairs[f"{a}-{b}"] += 1
    payload = hill_formula(counts) + "|" + ";".join(
        f"{k}:{v // 2}" for k, v in sorted(pairs.items())
    )
    return "CX" + hashlib.sha256(payload.encode()).hexdigest()[:22].upper()
