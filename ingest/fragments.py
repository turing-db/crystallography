"""SMARTS patterns for fragment perception, in one reviewable table.

The spec asks for these to live in a single file so a chemist can review them
together, which is also why each pattern carries a plain-English description of
what it is meant to match and, where relevant, what it deliberately does not.

Fragments are what make Q1 (synthon search) and Q3 (coformer discovery) work:
Q1 looks for two `carboxylic_acid` fragments hydrogen bonded to each other in
both directions, and Q3 finds molecules that share fragments with a target.

Matching runs on the RDKit molecule perceived for each connected component. A
component whose perception fails contributes no fragments and is counted in the
ingest report, because a high failure rate on metal-organics would make Q1 and
Q3 look thin and we need to know that number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class FragmentPattern:
    """One perceivable chemical fragment."""

    name: str
    smarts: str
    description: str
    #: Set when the pattern is knowingly narrower or broader than the name
    #: suggests, so reviewers see the caveat next to the pattern.
    caveat: str = ""


#: Order is not significant; every pattern is tried against every component.
FRAGMENT_PATTERNS: Final[tuple[FragmentPattern, ...]] = (
    FragmentPattern(
        name="carboxylic_acid",
        smarts="[CX3](=[OX1])[OX2H1]",
        description=(
            "Neutral carboxylic acid: trigonal carbon, one double-bonded "
            "oxygen, one hydroxyl oxygen carrying its hydrogen."
        ),
        caveat=(
            "Requires an explicit H on the hydroxyl oxygen, so acids in "
            "structures refined without hydrogen positions will NOT match. "
            "carboxylate is a separate pattern; the acid dimer synthon of Q1 "
            "is defined on the neutral form."
        ),
    ),
    FragmentPattern(
        name="carboxylate",
        smarts="[CX3](=[OX1])[OX1H0-]",
        description="Deprotonated carboxylate anion, as found in salts.",
        caveat="Kept distinct from carboxylic_acid so salt/cocrystal can be told apart.",
    ),
    FragmentPattern(
        name="amide_primary",
        smarts="[CX3](=[OX1])[NX3H2]",
        description="Primary amide, C(=O)NH2.",
    ),
    FragmentPattern(
        name="amide_secondary",
        smarts="[CX3](=[OX1])[NX3H1][#6]",
        description="Secondary amide, C(=O)NH-C.",
    ),
    FragmentPattern(
        name="amide_tertiary",
        smarts="[CX3](=[OX1])[NX3H0]([#6])[#6]",
        description="Tertiary amide, C(=O)N(C)C.",
    ),
    FragmentPattern(
        name="pyridine_nitrogen",
        smarts="[nX2r6;$(n1ccccc1)]",
        description=(
            "Aromatic nitrogen in a six-membered ring with no substituent on "
            "the nitrogen, i.e. a pyridine-type acceptor."
        ),
        caveat=(
            "Excludes pyridinium (protonated) and N-oxides. This is the "
            "classic acid/pyridine heterosynthon partner."
        ),
    ),
    FragmentPattern(
        name="hydroxyl",
        smarts="[OX2H][#6]",
        description="Alcohol or phenol hydroxyl bonded to carbon.",
        caveat="Excludes water and carboxylic-acid OH (the latter matches its own pattern too).",
    ),
    FragmentPattern(
        name="amine_primary",
        smarts="[NX3H2;!$(NC=O);!$(N[a])]",
        description="Primary aliphatic amine.",
        caveat="Excludes primary amides and anilines, which behave differently as donors.",
    ),
    FragmentPattern(
        name="sulfonamide",
        smarts="[SX4](=[OX1])(=[OX1])[NX3]",
        description="Sulfonamide, S(=O)(=O)N.",
    ),
    FragmentPattern(
        name="halide",
        smarts="[F,Cl,Br,I;X1][#6]",
        description="Halogen substituent bonded to carbon.",
        caveat=(
            "Covalently bound organohalogen only. Halide counter-IONS are not "
            "matched here; they appear as their own component."
        ),
    ),
    FragmentPattern(
        name="nitro",
        smarts="[NX3](=[OX1])=[OX1]",
        description="Nitro group in the neutral pentavalent representation.",
        caveat=(
            "RDKit may perceive nitro in the charge-separated form "
            "[N+](=O)[O-]; nitro_charged covers that case."
        ),
    ),
    FragmentPattern(
        name="nitro_charged",
        smarts="[NX3+](=[OX1])[OX1-]",
        description="Nitro group in the charge-separated representation.",
        caveat="Same chemical group as nitro; both are emitted so neither spelling is missed.",
    ),
    FragmentPattern(
        name="carbonyl",
        smarts="[CX3]=[OX1]",
        description="Any carbonyl carbon.",
        caveat=(
            "Deliberately broad: also matches the carbonyl inside acids, "
            "amides, esters and ketones. Useful as a coarse acceptor class, "
            "but do not treat a carbonyl count as a ketone count."
        ),
    ),
    FragmentPattern(
        name="aromatic_ring",
        smarts="a1aaaaa1",
        description="Six-membered aromatic ring, carbocyclic or heterocyclic.",
        caveat=(
            "Six-membered only. Five-membered aromatics are matched by "
            "aromatic_ring_5."
        ),
    ),
    FragmentPattern(
        name="aromatic_ring_5",
        smarts="a1aaaa1",
        description="Five-membered aromatic ring (furan, thiophene, imidazole, ...).",
    ),
    FragmentPattern(
        name="ether",
        smarts="[OX2]([#6])[#6]",
        description="Dialkyl or aryl ether oxygen.",
        caveat="Also matches ester single-bonded oxygen; ester has its own pattern.",
    ),
    FragmentPattern(
        name="ester",
        smarts="[CX3](=[OX1])[OX2][#6]",
        description="Carboxylic ester.",
    ),
)

FRAGMENTS_BY_NAME: Final[dict[str, FragmentPattern]] = {
    f.name: f for f in FRAGMENT_PATTERNS
}


def compiled_patterns() -> list[tuple[FragmentPattern, object]]:
    """Compile every SMARTS with RDKit, once.

    Returns (pattern, RDKit Mol) pairs. Raises if a pattern fails to compile,
    because a silently dead SMARTS would quietly hollow out Q1 and Q3 rather
    than fail loudly.
    """
    from rdkit import Chem

    out: list[tuple[FragmentPattern, object]] = []
    bad: list[str] = []
    for pattern in FRAGMENT_PATTERNS:
        mol = Chem.MolFromSmarts(pattern.smarts)
        if mol is None:
            bad.append(f"{pattern.name}: {pattern.smarts}")
        else:
            out.append((pattern, mol))
    if bad:
        raise ValueError("SMARTS patterns failed to compile: " + "; ".join(bad))
    return out
