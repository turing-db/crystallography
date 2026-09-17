"""Explicit symmetry expansion and neighbour search.

We build the periodic images ourselves rather than using gemmi's
`NeighborSearch.find_site_neighbors`. Two reasons, one practical and one about
provenance:

* **Practical.** `find_site_neighbors` was observed to miss genuine close
  contacts on real COD entries -- e.g. for COD 2211937 it reported H9A's nearest
  neighbour as C8 at 2.06 A when H9A is covalently bonded to C9 at ~0.97 A --
  which silently fragmented molecules into single atoms.

* **Provenance.** Generating each image from a known (operation index, lattice
  translation) means the `symop` on every bond and contact edge is known *by
  construction*, not reverse-engineered from a position afterwards. The
  round-trip check in `ingest.symmetry.verify_symop` then becomes a real
  independent test rather than a tautology.

Expansion covers the 3x3x3 block of unit cells around the reference cell, which
is sufficient for any contact criterion here: the widest cutoff is 6 A and the
shortest cell edge in the slice comfortably exceeds half that after the block is
applied. Images are pruned to a bounding box around the asymmetric unit so the
KD-tree stays small.
"""

from __future__ import annotations

from dataclasses import dataclass

import gemmi
import numpy as np
from scipy.spatial import cKDTree

from .symmetry import SymOp

#: Two images of the SAME site closer than this are treated as one atom on a
#: special position. Distinct atoms are never merged (the key includes the site
#: index), so this only has to absorb the refinement noise around an exact
#: special position -- hence a value well below any real interatomic distance.
SPECIAL_POSITION_TOLERANCE = 0.30  # angstrom


@dataclass(frozen=True)
class Image:
    """One symmetry image of an asymmetric-unit site."""

    site_index: int
    op_index: int                      # 1-based, CIF convention
    translation: tuple[int, int, int]
    triplet: str

    def symop(self) -> SymOp:
        return SymOp(self.op_index, self.translation, self.triplet)


class PeriodicNeighbours:
    """Symmetry images of an asymmetric unit, with a KD-tree over their positions."""

    def __init__(
        self,
        st: gemmi.SmallStructure,
        sg: gemmi.SpaceGroup,
        fracts: np.ndarray,
        cutoff: float,
        max_images: int = 400_000,
    ) -> None:
        self.cell = st.cell
        self.sg = sg
        self.cutoff = cutoff
        self.truncated = False

        ops = list(sg.operations())
        n_sites = len(fracts)

        # Guard against pathological cases (very high symmetry x many sites).
        # This has to be a HARD stop, not just a flag: a cubic group has up to
        # 192 operations, so a few hundred sites across the 3x3x3 block is
        # millions of images and the worker never returns. Callers check
        # `.truncated` and skip the structure rather than trust a partial
        # expansion, because a partial expansion would silently lose bonds.
        self.truncated = n_sites * len(ops) * 27 > max_images

        rows: list[tuple[int, int, int, int, int]] = []
        coords: list[tuple[float, float, float]] = []
        # Atoms on a SPECIAL POSITION (an inversion centre, rotation axis or
        # mirror) are mapped onto themselves by more than one symmetry operation,
        # so the naive expansion emits several coincident images of the same
        # site. Left in, they produce one duplicate bond per coincident image --
        # observed as an oxygen with 24 identical Zn "bonds" at 1.94 A. We keep
        # only the first image of a site at any given point. Keying on the site
        # index means two genuinely distinct atoms are never merged, so the
        # tolerance only has to absorb refinement noise about the exact
        # special position.
        inv_tol = 1.0 / SPECIAL_POSITION_TOLERANCE

        # bounding box of the asymmetric unit in fractional space, padded by the
        # cutoff expressed in fractional units along each axis
        pad = np.array([
            cutoff / max(self.cell.a, 1e-6),
            cutoff / max(self.cell.b, 1e-6),
            cutoff / max(self.cell.c, 1e-6),
        ])
        lo = fracts.min(axis=0) - pad
        hi = fracts.max(axis=0) + pad

        if self.truncated:
            # Emit nothing. A partial expansion is worse than none: it would
            # drop real bonds and silently fragment molecules.
            self.meta = np.zeros((0, 5), np.int32)
            self.xyz = np.zeros((0, 3))
            self.triplets = [op.triplet() for op in ops]
            self.tree = None
            return
        # Vectorised expansion. The scalar triple loop with a gemmi call per
        # image dominated ingest time (a 6 A contact cutoff on a 10 A cell makes
        # the padded box span most of the 3x3x3 block, so it is thousands of
        # images per structure, built twice). Doing it in numpy is the
        # difference between ~4 structures/s and a usable rate.
        #
        # A gemmi Op stores its rotation and translation as integers scaled by
        # Op.DEN, so the fractional image is (R @ f + t) / DEN.
        den = float(gemmi.Op.DEN)
        orth = np.asarray(self.cell.orth.mat.tolist(), dtype=np.float64)

        offsets = np.array(
            [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)],
            dtype=np.float64,
        )
        site_idx = np.arange(n_sites)

        all_rows: list[np.ndarray] = []
        all_xyz: list[np.ndarray] = []
        for oi, op in enumerate(ops, start=1):
            rot = np.asarray(op.rot, dtype=np.float64) / den
            tran = np.asarray(op.tran, dtype=np.float64) / den
            raw = fracts @ rot.T + tran                    # (n_sites, 3)
            fl = np.floor(raw)
            wrapped = raw - fl                             # into [0, 1)

            # broadcast the 27 lattice offsets over every site
            pts = wrapped[:, None, :] + offsets[None, :, :]        # (n, 27, 3)
            trans = offsets[None, :, :] - fl[:, None, :]           # (n, 27, 3)

            inside = np.all((pts >= lo) & (pts <= hi), axis=2)      # (n, 27)
            if not inside.any():
                continue
            si_sel, off_sel = np.nonzero(inside)
            sel_pts = pts[si_sel, off_sel]                          # (k, 3)
            sel_tr = trans[si_sel, off_sel]
            xyz = sel_pts @ orth.T

            block = np.empty((len(si_sel), 5), dtype=np.int32)
            block[:, 0] = site_idx[si_sel]
            block[:, 1] = oi
            block[:, 2:5] = np.rint(sel_tr).astype(np.int32)
            all_rows.append(block)
            all_xyz.append(xyz)

        if all_rows:
            meta = np.concatenate(all_rows)
            xyz = np.concatenate(all_xyz)
            # Drop coincident images of the SAME site (special positions).
            keys = np.column_stack([
                meta[:, 0].astype(np.int64),
                np.rint(xyz * inv_tol).astype(np.int64),
            ])
            _, keep = np.unique(keys, axis=0, return_index=True)
            keep.sort()
            rows_arr = meta[keep]
            xyz_arr = xyz[keep]
        else:
            rows_arr = np.zeros((0, 5), np.int32)
            xyz_arr = np.zeros((0, 3))

        self.meta = rows_arr
        self.xyz = xyz_arr
        self.triplets = [op.triplet() for op in ops]
        self.tree = cKDTree(self.xyz) if len(self.xyz) else None
        return

        self.meta = np.asarray(rows, dtype=np.int32) if rows else np.zeros((0, 5), np.int32)
        self.xyz = np.asarray(coords, dtype=np.float64) if coords else np.zeros((0, 3))
        self.triplets = [op.triplet() for op in ops]
        self.tree = cKDTree(self.xyz) if len(self.xyz) else None

    def image_at(self, row: int) -> Image:
        si, oi, tx, ty, tz = (int(v) for v in self.meta[row])
        return Image(si, oi, (tx, ty, tz), self.triplets[oi - 1])

    def query(self, positions: np.ndarray, radius: float) -> list[list[int]]:
        """Rows of `meta` within `radius` of each query position."""
        if self.tree is None:
            return [[] for _ in positions]
        return self.tree.query_ball_point(positions, r=radius)


def asymmetric_positions(
    cell: gemmi.UnitCell, fracts: np.ndarray
) -> np.ndarray:
    """Cartesian coordinates of the asymmetric-unit sites."""
    out = np.empty((len(fracts), 3), dtype=np.float64)
    for i, (fx, fy, fz) in enumerate(fracts):
        p = cell.orthogonalize(gemmi.Fractional(float(fx), float(fy), float(fz)))
        out[i] = (p.x, p.y, p.z)
    return out
