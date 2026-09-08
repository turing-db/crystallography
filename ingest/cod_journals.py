"""Journal name table for selecting the demo slice from COD.

`cod.data.journal` is a free-text `varchar(255)` with no ISSN column and no
foreign key to the `journals` table, so journal selection is string matching and
the spellings genuinely vary. The strings below were counted from COD's live
metadata on 2026-09-03; the counts are recorded so that a future run can notice
if a new variant has appeared.

Do NOT replace these with a `LIKE '%Crystal Growth%'` pattern: that also catches
`Journal of Crystal Growth` and `Progress in Crystal Growth and Characterization`,
which are different journals, and the `To be published in ...` placeholder
journals COD uses for prepublication deposits.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Journal:
    """One target journal and every spelling COD stores for it."""

    key: str
    display: str
    #: exact strings seen in `cod.data.journal`, with their observed row counts
    variants: dict[str, int]
    note: str = ""

    @property
    def expected_rows(self) -> int:
        return sum(self.variants.values())


# Observed 2026-09-03 against COD's live metadata. Counts include duplicates,
# error-flagged and theoretical entries; the default COD web filter removes some.
JOURNALS: tuple[Journal, ...] = (
    Journal(
        key="acta_cryst_e",
        display="Acta Crystallographica Section E",
        variants={
            "Acta Crystallographica Section E": 43792,
            "Acta Crystallographica Section E Crystallographic Communications": 543,
            "Acta Crystallographica, Section E": 381,
            "Acta Crystallographica, Section E: Structure Reports Online": 38,
            "Acta Crystallographica Section E Structure Reports Online": 18,
            "Acta Crystallographica, Section e": 1,
        },
        note=(
            "Renamed in 2015 from 'Structure Reports Online' to 'Crystallographic "
            "Communications', so it spans several journals.id values (8, 1449, "
            "1387, 1388). Note the lowercase 'Section e' typo in one row."
        ),
    ),
    Journal(
        key="crystengcomm",
        display="CrystEngComm",
        variants={
            "CrystEngComm": 31169,
            "CrystEngComm / RSC": 15,
        },
    ),
    Journal(
        key="crystal_growth_design",
        display="Crystal Growth & Design",
        variants={
            "Crystal Growth & Design": 13620,
            "Cryst. Growth & Design": 91,
            "Crystal growth & design": 74,
            "Crystal Growth and Design": 26,
            "Crystal growth and Design": 17,
            "Crystal growth & Design": 13,
        },
        note="Case and '&' vs 'and' both vary; match case-insensitively.",
    ),
)

JOURNALS_BY_KEY: dict[str, Journal] = {j.key: j for j in JOURNALS}


def all_variants() -> list[str]:
    """Every accepted journal string across all target journals."""
    out: list[str] = []
    for journal in JOURNALS:
        out.extend(journal.variants)
    return out


def journal_key_for(raw: str | None) -> str | None:
    """Map a raw `cod.data.journal` value onto one of our journal keys.

    Case-insensitive and whitespace-insensitive, because the stored strings are
    inconsistent about both. Returns None for journals outside the slice.
    """
    if not raw:
        return None
    needle = " ".join(raw.split()).casefold()
    for journal in JOURNALS:
        for variant in journal.variants:
            if " ".join(variant.split()).casefold() == needle:
                return journal.key
    return None
