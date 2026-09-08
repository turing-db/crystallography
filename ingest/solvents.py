"""Solvent and counter-ion recognition, in one reviewable table.

`role` on CONTAINS_COMPONENT is what stops coformer discovery from answering
"water" for every query, so it is worth getting right and worth being explicit
about. This is a chemical judgement call and is worth a chemist's review.

Matching is by molecular formula in Hill notation, which is safe here because
solvents are small and their formulae are unambiguous at this size. InChIKey
would be stricter, but perception fails often enough on small disordered
solvates that formula matching recovers more of them.

Roles assigned:
  principal   the largest non-solvent, non-counter-ion component by heavy-atom
              count -- the molecule the paper is about
  coformer    any other neutral non-solvent component (the interesting case:
              co-crystals)
  solvent     matches SOLVENT_FORMULAE
  counter_ion matches ION_FORMULAE, or is a single-atom charged species
"""

from __future__ import annotations

from typing import Final

#: Formula (Hill notation, as produced by structure.hill_formula) -> common name.
#: Hydrogens are frequently unrefined on solvents, so the anhydrous heavy-atom
#: skeleton is listed alongside the fully protonated formula where they differ.
SOLVENT_FORMULAE: Final[dict[str, str]] = {
    "H2 O": "water",
    "O": "water (H not located)",
    "C H4 O": "methanol",
    "C O": "methanol (H not located)",
    "C2 H6 O": "ethanol",
    "C2 O": "ethanol (H not located)",
    "C3 H8 O": "propanol / isopropanol",
    "C3 O": "propanol (H not located)",
    "C2 H6 O S": "dimethyl sulfoxide",
    "C2 O S": "DMSO (H not located)",
    "C3 H7 N O": "dimethylformamide",
    "C3 N O": "DMF (H not located)",
    "C2 H3 N": "acetonitrile",
    "C2 N": "acetonitrile (H not located)",
    "C3 H6 O": "acetone",
    "C3 O": "acetone (H not located)",
    "C4 H8 O": "tetrahydrofuran",
    "C4 O": "THF (H not located)",
    "C4 H10 O": "diethyl ether",
    "C4 H8 O2": "dioxane / ethyl acetate",
    "C H2 Cl2": "dichloromethane",
    "C Cl2": "DCM (H not located)",
    "C H Cl3": "chloroform",
    "C Cl3": "chloroform (H not located)",
    "C Cl4": "carbon tetrachloride",
    "C6 H6": "benzene",
    "C7 H8": "toluene",
    "C6 H14": "hexane",
    "C5 H12": "pentane",
    "C6 H12": "cyclohexane",
    "C2 H4 Cl2": "dichloroethane",
    "C6 H5 Cl": "chlorobenzene",
    "C H2 O2": "formic acid",
    "C2 H4 O2": "acetic acid",
    "C5 H5 N": "pyridine",
    "H3 N": "ammonia",
    "N": "ammonia (H not located)",
}

#: Common counter-ions, again by Hill formula.
ION_FORMULAE: Final[dict[str, str]] = {
    "Cl": "chloride",
    "Br": "bromide",
    "I": "iodide",
    "F": "fluoride",
    "Cl O4": "perchlorate",
    "Cl O3": "chlorate",
    "B F4": "tetrafluoroborate",
    "F6 P": "hexafluorophosphate",
    "F6 Sb": "hexafluoroantimonate",
    "N O3": "nitrate",
    "N O2": "nitrite",
    "O4 S": "sulfate",
    "H O4 S": "hydrogensulfate",
    "O4 P": "phosphate",
    "C N": "cyanide",
    "C N S": "thiocyanate",
    "F3 O3 S": "triflate",
    "C H O2": "formate",
    "C2 H3 O2": "acetate",
    "Na": "sodium",
    "K": "potassium",
    "Li": "lithium",
    "Cs": "caesium",
    "Rb": "rubidium",
    "O H": "hydroxide",
}

#: Elements that are essentially always counter-ions when they appear alone.
MONATOMIC_IONS: Final[frozenset[str]] = frozenset(
    {"Cl", "Br", "I", "F", "Na", "K", "Li", "Cs", "Rb", "Ca", "Mg", "Ba", "Sr"}
)


def classify(formula: str, n_heavy: int, n_atoms: int) -> str:
    """Return 'solvent', 'counter_ion' or '' (meaning: decide from context)."""
    if formula in SOLVENT_FORMULAE:
        return "solvent"
    if formula in ION_FORMULAE:
        return "counter_ion"
    if n_atoms == 1 and formula in MONATOMIC_IONS:
        return "counter_ion"
    return ""


def solvent_name(formula: str) -> str:
    return SOLVENT_FORMULAE.get(formula) or ION_FORMULAE.get(formula) or ""


def assign_roles(components: list[tuple[str, int, int]]) -> list[str]:
    """Assign a role to every component of one structure.

    `components` is [(formula, n_heavy, n_atoms)] in order. The largest
    remaining non-solvent, non-ion component becomes `principal`; any other
    such component becomes `coformer`, which is what makes the co-crystal
    question meaningful rather than returning water for everything.
    """
    roles: list[str] = []
    for formula, n_heavy, n_atoms in components:
        roles.append(classify(formula, n_heavy, n_atoms))

    candidates = [
        i for i, r in enumerate(roles)
        if not r and components[i][1] > 0        # has at least one heavy atom
    ]
    if candidates:
        principal = max(candidates, key=lambda i: (components[i][1], components[i][2]))
        for i in candidates:
            roles[i] = "principal" if i == principal else "coformer"
    # anything still unlabelled had no heavy atoms at all
    return [r or "other" for r in roles]
