"""Radii tables and geometric criteria, with the convention cited for each.

Chemistry correctness is non-negotiable for this demo, so every cutoff used
anywhere in the ingest is defined here, once, with its literature source. A
chemist should be able to review this single file and know exactly what the
pipeline does.

We deliberately do NOT use gemmi's built-in `Element.covalent_r` / `vdw_r`.
Their provenance is mixed: gemmi reports C = 0.73 (Cordero's sp2 carbon, not the
0.76 sp3 value usually tabulated) and Fe vdW = 1.26, which is not a Bondi value
at all -- Bondi gives no vdW radius for iron. Shipping our own tables keeps the
citation honest and makes missing values explicit instead of silently invented.

Sources
-------
COVALENT_RADII
    Cordero, B. et al. "Covalent radii revisited."
    Dalton Trans., 2008, 2832-2838.  doi:10.1039/B801115J
    Values in angstrom. For Mn, Fe and Co, Cordero tabulates both low-spin and
    high-spin radii; see SPIN_STATE_NOTE below for which we take and why.

VDW_RADII
    Bondi, A. "van der Waals Volumes and Radii."
    J. Phys. Chem., 1964, 68, 441-451.  doi:10.1021/j100785a001
    Extended for a few main-group elements Bondi omitted using
    Mantina, M. et al. J. Phys. Chem. A, 2009, 113, 5806-5812
    doi:10.1021/jp8111556 -- these are marked in VDW_SOURCE.
    Elements with no published value in either source are ABSENT from the table
    and `vdw_radius()` returns None, so callers must decide explicitly rather
    than silently using a wrong number.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------
# covalent radii -- Cordero et al., Dalton Trans., 2008, 2832
# --------------------------------------------------------------------------

#: Cordero gives two radii for Mn, Fe and Co (low spin / high spin). We take the
#: HIGH-SPIN value, because the great majority of first-row transition-metal
#: complexes in a small-molecule structural database are high spin at the
#: temperatures COD entries are collected at, and because under-estimating the
#: radius would silently MISS metal-ligand bonds, which fragments the component
#: graph and is the more damaging error for this demo. Flagged in
#: worth a chemist's review.
SPIN_STATE_NOTE: Final = "Mn/Fe/Co use Cordero high-spin radii"

COVALENT_RADII: Final[dict[str, float]] = {
    "H": 0.31, "He": 0.28,
    "Li": 1.28, "Be": 0.96, "B": 0.84, "C": 0.76, "N": 0.71, "O": 0.66,
    "F": 0.57, "Ne": 0.58,
    "Na": 1.66, "Mg": 1.41, "Al": 1.21, "Si": 1.11, "P": 1.07, "S": 1.05,
    "Cl": 1.02, "Ar": 1.06,
    "K": 2.03, "Ca": 1.76, "Sc": 1.70, "Ti": 1.60, "V": 1.53, "Cr": 1.39,
    "Mn": 1.61, "Fe": 1.52, "Co": 1.50,          # high spin -- see SPIN_STATE_NOTE
    "Ni": 1.24, "Cu": 1.32, "Zn": 1.22, "Ga": 1.22, "Ge": 1.20, "As": 1.19,
    "Se": 1.20, "Br": 1.20, "Kr": 1.16,
    "Rb": 2.20, "Sr": 1.95, "Y": 1.90, "Zr": 1.75, "Nb": 1.64, "Mo": 1.54,
    "Tc": 1.47, "Ru": 1.46, "Rh": 1.42, "Pd": 1.39, "Ag": 1.45, "Cd": 1.44,
    "In": 1.42, "Sn": 1.39, "Sb": 1.39, "Te": 1.38, "I": 1.39, "Xe": 1.40,
    "Cs": 2.44, "Ba": 2.15,
    "La": 2.07, "Ce": 2.04, "Pr": 2.03, "Nd": 2.01, "Pm": 1.99, "Sm": 1.98,
    "Eu": 1.98, "Gd": 1.96, "Tb": 1.94, "Dy": 1.92, "Ho": 1.92, "Er": 1.89,
    "Tm": 1.90, "Yb": 1.87, "Lu": 1.87,
    "Hf": 1.75, "Ta": 1.70, "W": 1.62, "Re": 1.51, "Os": 1.44, "Ir": 1.41,
    "Pt": 1.36, "Au": 1.36, "Hg": 1.32, "Tl": 1.45, "Pb": 1.46, "Bi": 1.48,
    "Po": 1.40, "At": 1.50, "Rn": 1.50,
    "Fr": 2.60, "Ra": 2.21,
    "Ac": 2.15, "Th": 2.06, "Pa": 2.00, "U": 1.96, "Np": 1.90, "Pu": 1.87,
    "Am": 1.80, "Cm": 1.69,
}

# --------------------------------------------------------------------------
# van der Waals radii -- Bondi 1964, extended by Mantina 2009
# --------------------------------------------------------------------------

VDW_RADII: Final[dict[str, float]] = {
    # Bondi 1964
    "H": 1.20, "He": 1.40,
    "Li": 1.82, "C": 1.70, "N": 1.55, "O": 1.52, "F": 1.47, "Ne": 1.54,
    "Na": 2.27, "Mg": 1.73, "Si": 2.10, "P": 1.80, "S": 1.80, "Cl": 1.75,
    "Ar": 1.88,
    "K": 2.75, "Ni": 1.63, "Cu": 1.40, "Zn": 1.39, "Ga": 1.87, "As": 1.85,
    "Se": 1.90, "Br": 1.85, "Kr": 2.02,
    "Pd": 1.63, "Ag": 1.72, "Cd": 1.58, "In": 1.93, "Sn": 2.17, "Te": 2.06,
    "I": 1.98, "Xe": 2.16,
    "Pt": 1.75, "Au": 1.66, "Hg": 1.55, "Tl": 1.96, "Pb": 2.02, "U": 1.86,
    # Mantina 2009 additions for main-group elements Bondi omitted
    "Be": 1.53, "B": 1.92, "Al": 1.84, "Ca": 2.31, "Ge": 2.11, "Rb": 3.03,
    "Sr": 2.49, "Sb": 2.06, "Cs": 3.43, "Ba": 2.68, "Bi": 2.07, "Po": 1.97,
    "At": 2.02, "Rn": 2.20,
}

VDW_SOURCE: Final[dict[str, str]] = {
    el: ("Mantina2009" if el in {
        "Be", "B", "Al", "Ca", "Ge", "Rb", "Sr", "Sb", "Cs", "Ba", "Bi",
        "Po", "At", "Rn",
    } else "Bondi1964")
    for el in VDW_RADII
}

# --------------------------------------------------------------------------
# geometric criteria
# --------------------------------------------------------------------------

#: Covalent bond tolerance, per the project spec: a bond exists where
#: d(A,B) < r_cov(A) + r_cov(B) + BOND_TOLERANCE.
BOND_TOLERANCE: Final = 0.40

#: Sites with occupancy below this are excluded from bonding, so that disordered
#: partial sites -- which frequently sit within bonding distance of the
#: alternative component of the same disorder -- are not bonded together.
MIN_BONDING_OCCUPANCY: Final = 0.50

#: Hydrogen bond, H present: H...A distance and D-H...A angle.
HBOND_MAX_H_ACCEPTOR: Final = 2.50          # angstrom
HBOND_MIN_ANGLE: Final = 120.0              # degrees

#: Hydrogen bond, H absent. Many COD entries have no hydrogen positions at all
#: (Acta E in particular). Where the donor's H is missing we fall back to a
#: heavy-atom donor...acceptor distance and mark the edge h_inferred=True so the
#: two populations are never silently mixed.
HBOND_HEAVY_MAX: Final = 3.50               # angstrom

#: Elements admitted as hydrogen-bond donor or acceptor heavy atoms.
HBOND_ELEMENTS: Final[frozenset[str]] = frozenset({"N", "O", "F", "S", "Cl"})

#: Halogen bond: C-X...A where X is Cl, Br or I. The X...A distance must be
#: below the sum of van der Waals radii, and the C-X...A angle above 150 deg
#: (the sigma-hole is directed along the C-X axis).
HALOGEN_DONORS: Final[frozenset[str]] = frozenset({"Cl", "Br", "I"})
HALOGEN_MIN_ANGLE: Final = 150.0            # degrees

#: pi-stacking: centroid-centroid distance and interplanar angle between two
#: aromatic rings.
PI_STACK_MAX_CENTROID: Final = 4.00         # angstrom
PI_STACK_MAX_INTERPLANAR: Final = 30.0      # degrees

#: Widest cutoff any criterion needs, used to size the neighbour search once.
MAX_CONTACT_SEARCH_RADIUS: Final = 6.00     # angstrom


# --------------------------------------------------------------------------
# lookups
# --------------------------------------------------------------------------


def normalise_element(symbol: str) -> str:
    """Normalise a CIF `type_symbol` to a bare element symbol.

    CIF type symbols routinely carry an oxidation state or a disorder suffix:
    `Cu2+`, `O1-`, `Fe3+`. Strip anything that is not a letter and title-case
    the result, so `FE` and `fe` both become `Fe`.
    """
    letters = "".join(ch for ch in symbol if ch.isalpha())
    return letters[:1].upper() + letters[1:].lower() if letters else ""


def covalent_radius(symbol: str) -> float | None:
    """Cordero 2008 covalent radius in angstrom, or None if not tabulated."""
    return COVALENT_RADII.get(normalise_element(symbol))


def vdw_radius(symbol: str) -> float | None:
    """Bondi/Mantina van der Waals radius in angstrom, or None if not tabulated."""
    return VDW_RADII.get(normalise_element(symbol))


def bond_cutoff(symbol_a: str, symbol_b: str) -> float | None:
    """Maximum A-B distance counted as a covalent bond.

    r_cov(A) + r_cov(B) + BOND_TOLERANCE, per the spec. Returns None when either
    element has no tabulated covalent radius, so the caller can count the case
    in the ingest report rather than guessing a bond.
    """
    ra = covalent_radius(symbol_a)
    rb = covalent_radius(symbol_b)
    if ra is None or rb is None:
        return None
    return ra + rb + BOND_TOLERANCE


def vdw_sum(symbol_a: str, symbol_b: str) -> float | None:
    """Sum of van der Waals radii, or None if either is not tabulated."""
    ra = vdw_radius(symbol_a)
    rb = vdw_radius(symbol_b)
    if ra is None or rb is None:
        return None
    return ra + rb


def is_hbond_element(symbol: str) -> bool:
    return normalise_element(symbol) in HBOND_ELEMENTS


def is_halogen_donor(symbol: str) -> bool:
    return normalise_element(symbol) in HALOGEN_DONORS


#: Reported in the ingest report so the exact conventions travel with the data.
CRITERIA_PROVENANCE: Final[dict[str, str]] = {
    "covalent_radii": (
        "Cordero et al., Dalton Trans., 2008, 2832 (doi:10.1039/B801115J); "
        + SPIN_STATE_NOTE
    ),
    "vdw_radii": (
        "Bondi, J. Phys. Chem., 1964, 68, 441 (doi:10.1021/j100785a001), "
        "extended by Mantina et al., J. Phys. Chem. A, 2009, 113, 5806 "
        "(doi:10.1021/jp8111556)"
    ),
    "bond_rule": f"d < r_cov(A) + r_cov(B) + {BOND_TOLERANCE} A",
    "bond_occupancy_filter": f"sites with occupancy < {MIN_BONDING_OCCUPANCY} excluded",
    "hbond_with_h": (
        f"H...A < {HBOND_MAX_H_ACCEPTOR} A and D-H...A > {HBOND_MIN_ANGLE} deg, "
        f"D and A in {sorted(HBOND_ELEMENTS)}"
    ),
    "hbond_without_h": (
        f"D...A < {HBOND_HEAVY_MAX} A, flagged h_inferred=true"
    ),
    "halogen_bond": (
        f"X in {sorted(HALOGEN_DONORS)}; X...A < sum of vdW radii and "
        f"C-X...A > {HALOGEN_MIN_ANGLE} deg"
    ),
    "pi_stack": (
        f"ring centroid separation < {PI_STACK_MAX_CENTROID} A and interplanar "
        f"angle < {PI_STACK_MAX_INTERPLANAR} deg"
    ),
}
