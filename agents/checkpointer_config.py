"""
agents/checkpointer_config.py — LangGraph SQLite checkpoint persistence.

The checkpointer enables durable state: if the agent process crashes mid-pipeline,
re-invoking with the same `thread_id` resumes from the last completed node,
not from the beginning.

For production: replace SqliteSaver with PostgresSaver from
`langgraph-checkpoint-postgres`. The API is identical — only the import changes.

Usage:
    from agents.checkpointer_config import get_checkpointer
    graph = workflow.compile(checkpointer=get_checkpointer())
"""

import os
import logging
from langgraph.checkpoint.sqlite import SqliteSaver

log = logging.getLogger("checkpointer")

CHECKPOINT_DB_PATH = os.getenv(
    "SENTINELOPS_CHECKPOINT_DB",
    "sentinelops_checkpoints.db"
)


def get_checkpointer() -> SqliteSaver:
    """
    Return a SqliteSaver instance backed by a local SQLite database.

    The database file is created automatically if it doesn't exist.
    Thread-safe for concurrent graph invocations (SQLite WAL mode).

    To upgrade to Postgres for production:
        from langgraph.checkpoint.postgres import PostgresSaver
        import psycopg
        conn = psycopg.connect(os.environ["DATABASE_URL"])
        return PostgresSaver(conn)
    """
    log.info("Loading SQLite checkpointer from %s", CHECKPOINT_DB_PATH)
    return SqliteSaver.from_conn_string(CHECKPOINT_DB_PATH)
