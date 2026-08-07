"""
agents/checkpointer_config.py — LangGraph SQLite checkpoint persistence.

The checkpointer enables durable state: if the agent process crashes mid-pipeline,
re-invoking with the same `thread_id` resumes from the last completed node,
not from the beginning.

For production: replace SqliteSaver with PostgresSaver from
`langgraph-checkpoint-postgres`. The API is identical — only the import changes.

# ── BUG FIX NOTE ──────────────────────────────────────────────────────────────
# SqliteSaver.from_conn_string() is decorated with @contextmanager, meaning it
# returns a `_GeneratorContextManager` object, NOT a SqliteSaver instance.
# Passing that to workflow.compile(checkpointer=...) causes:
#   AttributeError: '_GeneratorContextManager' object has no attribute 'put'
#
# Fix: open the sqlite3.Connection directly and pass it to SqliteSaver(conn).
# This bypasses the context-manager wrapper entirely and gives us a real
# SqliteSaver instance that the module-level singleton in graph.py can hold
# for the lifetime of the process.
# ──────────────────────────────────────────────────────────────────────────────

Usage:
    from agents.checkpointer_config import get_checkpointer
    graph = workflow.compile(checkpointer=get_checkpointer())
"""

import os
import sqlite3
import logging
from pathlib import Path
from langgraph.checkpoint.sqlite import SqliteSaver

log = logging.getLogger("checkpointer")

CHECKPOINT_DB_PATH = os.getenv(
    "SENTINELOPS_CHECKPOINT_DB",
    "sentinelops_checkpoints.db"
)


def get_checkpointer() -> SqliteSaver:
    """
    Return a SqliteSaver instance backed by a local SQLite database.

    Uses the direct SqliteSaver(conn) constructor rather than the
    from_conn_string() classmethod, which is a @contextmanager and would
    return a _GeneratorContextManager instead of the actual SqliteSaver.

    check_same_thread=False is required for concurrent graph invocations
    from multiple threads (e.g. the webhook server dispatches each incident
    in a background thread).

    The connection is held open for the lifetime of the process — the OS
    will close it on exit. For explicit cleanup, wrap usage in a try/finally
    and call conn.close() after the graph is done.

    To upgrade to Postgres for production:
        from langgraph.checkpoint.postgres import PostgresSaver
        import psycopg
        conn = psycopg.connect(os.environ["DATABASE_URL"])
        return PostgresSaver(conn)
    """
    log.info("Opening SQLite checkpoint database at: %s", CHECKPOINT_DB_PATH)

    # Ensure the directory for the database file exists
    db_path = Path(CHECKPOINT_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Open the connection directly — check_same_thread=False is required
    # because the webhook server dispatches pipeline invocations from daemon
    # threads (one per incoming alert), and SQLite would otherwise raise:
    # "sqlite3.ProgrammingError: SQLite objects created in a thread can only
    #  be used in that same thread"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)

    saver = SqliteSaver(conn)
    log.info("SqliteSaver ready (type=%s)", type(saver).__name__)
    return saver
