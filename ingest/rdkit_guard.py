"""A hard timeout around RDKit's bond-order perception.

Why this exists
---------------
`rdDetermineBonds.DetermineBondOrders` searches over bond-order assignments and
on some connectivity graphs it does not terminate. It is a C++ call, so it never
returns to the interpreter and **no Python-level watchdog can stop it**: the
ingest already arms a 20 s `SIGALRM` per structure and the signal simply sits
undelivered while the process spins at 100% CPU.

An external `timeout` around the whole ingest does not solve it either: it
sends SIGTERM to a process that cannot act on one, so the child survives and
the wrapper is left supervising a process it cannot stop.

The timeout therefore has to live in a process the parent can actually *kill*.

Design
------
One long-lived worker process, `fork`-ed rather than spawned: spawn re-imports
`__main__` in the child, which misbehaves when the caller is not an importable
module. Work goes over a `Pipe`, and the parent waits with a deadline:

* answer inside the budget  -> return it, keep the worker
* deadline passes           -> SIGKILL the worker, respawn it lazily, report a
                               timeout for this component only

The worker is reused across calls, so the cost is one pipe round trip per
component (measured well under a millisecond) rather than a process spawn.

The molecule crosses the boundary as a mol block, which is the cheapest
representation RDKit will round-trip without needing the caller's RWMol.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from typing import Any

#: Budget for one component's bond-order perception. Generous next to the
#: sub-millisecond typical case, small next to "never returns".
PERCEPTION_TIMEOUT_S = 10.0


def _worker(conn: Any) -> None:  # pragma: no cover - runs in a child process
    """Perceive mol blocks until the pipe closes. Killed, never asked to stop."""
    from rdkit import Chem, RDLogger
    from rdkit.Chem import rdDetermineBonds

    RDLogger.DisableLog("rdApp.*")
    while True:
        try:
            molblock = conn.recv()
        except EOFError:
            return
        try:
            m = Chem.MolFromMolBlock(molblock, sanitize=False, removeHs=False)
            if m is None:
                conn.send(("error", "MolFromMolBlock returned None"))
                continue
            rdDetermineBonds.DetermineBondOrders(m, charge=0, embedChiral=False)
            Chem.SanitizeMol(m)
            inchi = Chem.MolToInchi(m)
            if not inchi:
                conn.send(("error", "empty InChI"))
                continue
            conn.send((
                "ok",
                {
                    "inchikey": Chem.InchiToInchiKey(inchi),
                    "charge": Chem.GetFormalCharge(m),
                    "smiles": Chem.MolToSmiles(m),
                },
            ))
        except Exception as exc:  # noqa: BLE001
            conn.send(("error", type(exc).__name__ + ": " + str(exc)[:120]))


class BondOrderPerceiver:
    """Perceive bond orders in a killable child process."""

    def __init__(self, timeout_s: float = PERCEPTION_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s
        self._proc: mp.process.BaseProcess | None = None
        self._conn: Any = None
        #: components abandoned because the child had to be killed
        self.timeouts = 0

    def _ensure(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            return
        # fork, not spawn: the child must not re-import __main__, and the work
        # is pure CPU holding no threads or open sockets.
        ctx = mp.get_context("fork")
        parent, child = ctx.Pipe()
        self._proc = ctx.Process(target=_worker, args=(child,), daemon=True)
        self._proc.start()
        child.close()
        self._conn = parent

    def _kill(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.pid:
                    os.kill(self._proc.pid, 9)
            except (ProcessLookupError, PermissionError, AttributeError):
                pass
            self._proc.join(timeout=2)
        self._proc = None
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = None

    def perceive(self, molblock: str) -> tuple[str, Any]:
        """Return ('ok', {...}) or ('error', reason). Never raises, never hangs."""
        try:
            self._ensure()
            self._conn.send(molblock)
            if not self._conn.poll(self.timeout_s):
                self._kill()
                self.timeouts += 1
                return "error", (
                    f"RDKitTimeout: bond-order perception exceeded "
                    f"{self.timeout_s:g}s and the worker was killed"
                )
            return self._conn.recv()  # type: ignore[no-any-return]
        except (EOFError, BrokenPipeError, OSError) as exc:
            # the child died mid-call; treat exactly like a failed perception
            self._kill()
            return "error", f"RDKitWorkerLost: {type(exc).__name__}: {exc}"[:160]

    def close(self) -> None:
        self._kill()
