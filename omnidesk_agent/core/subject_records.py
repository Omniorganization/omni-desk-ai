"""Shared pieces of the data-subject methods of the SQLite stores.

Each store that holds records about a person implements

    export_subject(actor) -> list[dict]   # JSON-safe records about that actor id
    erase_subject(actor) -> int           # records deleted or anonymised

for ``privacy.data_subject.DataSubjectService``. Erasing happens in
``erasure_transaction``, so the erased content does not stay readable in the
database files: secure_delete zeroes what the transaction frees, VACUUM then
rewrites the file without the free pages that earlier ordinary updates left
behind, and the write-ahead log is checkpointed and truncated, so earlier page
versions do not stay in the ``-wal`` file.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from omnidesk_agent.storage.sqlite import connect_sqlite

LOGGER = logging.getLogger(__name__)

# Written where a row must stay but must no longer name the person.
ERASED_ACTOR = "[erased]"


@contextmanager
def erasure_transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    """An IMMEDIATE write transaction on `db_path` whose deleted content is overwritten."""
    with connect_sqlite(db_path) as con:
        con.execute("PRAGMA secure_delete = ON")
        con.execute("BEGIN IMMEDIATE")
        yield con
    with connect_sqlite(db_path) as con:
        try:
            con.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            # The erased rows are gone and the pages they used were zeroed; only free
            # pages from before this erasure stay until the next VACUUM.
            LOGGER.warning("VACUUM after a data-subject erasure did not run on %s: %s", db_path, exc)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def json_object(text: Any) -> dict[str, Any]:
    """`text` parsed as a JSON object, or {} when it is not one."""
    try:
        value = json.loads(text) if isinstance(text, (str, bytes)) else text
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
