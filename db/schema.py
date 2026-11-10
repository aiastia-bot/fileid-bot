"""Versioned, repeatable upgrades. Run once before accepting traffic.

Legacy duplicate file codes are preserved: only the redundant identity is cleared.
Historical payment rows are never deleted or rewritten; payment_orders is the new
unique transaction ledger.
"""

from datetime import datetime
from sqlalchemy import inspect, select, text
from sqlalchemy.schema import CreateColumn
from db.models import Base, SchemaVersion


async def upgrade(conn):
    await conn.run_sync(Base.metadata.create_all)
    applied = set((await conn.execute(select(SchemaVersion.version))).scalars())
    if 1 not in applied:
        await conn.run_sync(_columns)
        await conn.execute(
            SchemaVersion.__table__.insert().values(
                version=1, applied_at=datetime.now().isoformat(timespec="seconds")
            )
        )
    if 2 not in applied:
        await conn.execute(
            text("UPDATE file_mappings SET file_unique_id=NULL WHERE file_unique_id=''")
        )
        # Keep every public code and collection reference working.
        rows = (
            await conn.execute(
                text(
                    "SELECT bot_db_id, file_unique_id, MIN(id) AS keep_id FROM file_mappings "
                    "WHERE bot_db_id IS NOT NULL AND file_unique_id IS NOT NULL "
                    "GROUP BY bot_db_id, file_unique_id HAVING COUNT(*) > 1"
                )
            )
        ).fetchall()
        for bid, identity, keep in rows:
            await conn.execute(
                text(
                    "UPDATE file_mappings SET file_unique_id=NULL "
                    "WHERE bot_db_id=:bid AND file_unique_id=:identity AND id<>:keep"
                ),
                {"bid": bid, "identity": identity, "keep": keep},
            )
        await conn.run_sync(_indexes)
        await conn.execute(
            SchemaVersion.__table__.insert().values(
                version=2, applied_at=datetime.now().isoformat(timespec="seconds")
            )
        )
    if 3 not in applied:
        await conn.execute(text("DELETE FROM bot_file_counts"))
        await conn.execute(
            text(
                "INSERT INTO bot_file_counts (bot_db_id, count) "
                "SELECT bot_db_id, COUNT(*) FROM file_mappings "
                "WHERE bot_db_id IS NOT NULL GROUP BY bot_db_id"
            )
        )
        await conn.run_sync(_count_triggers)
        await conn.execute(
            SchemaVersion.__table__.insert().values(
                version=3, applied_at=datetime.now().isoformat(timespec="seconds")
            )
        )


def _columns(conn):
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"]: c for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing:
                # Legacy tables only acquire nullable columns; backfill app defaults.
                ddl = str(CreateColumn(column).compile(dialect=conn.dialect))
                ddl = ddl.replace(" NOT NULL", "")
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
                if column.default is not None and column.default.is_scalar:
                    conn.execute(
                        table.update()
                        .where(column.is_(None))
                        .values({column.name: column.default.arg})
                    )
            elif conn.dialect.name == "mysql":
                actual = existing[column.name]["type"]
                # Upgrade int IDs without shrinking any existing string columns.
                from sqlalchemy import BigInteger

                if isinstance(column.type, BigInteger) and not isinstance(
                    actual, BigInteger
                ):
                    null = "NULL" if column.nullable else "NOT NULL"
                    conn.execute(
                        text(
                            f"ALTER TABLE {table.name} MODIFY COLUMN {column.name} BIGINT {null}"
                        )
                    )


def _indexes(conn):
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(conn, checkfirst=True)
    constraints = inspector.get_unique_constraints("file_mappings")
    indexes = inspector.get_indexes("file_mappings")
    if not any(
        c.get("column_names") == ["bot_db_id", "file_unique_id"]
        for c in constraints + [i for i in indexes if i.get("unique")]
    ):
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_file_bot_identity ON file_mappings(bot_db_id, file_unique_id)"
            )
        )
    for name, table, cols in (
        ("idx_file_code", "file_mappings", "code"),
        ("idx_file_bot_db", "file_mappings", "bot_db_id"),
        ("idx_ub_owner", "user_bots", "owner_id"),
        ("idx_ci_col", "collection_items", "collection_code"),
        ("idx_col_bot_db", "collections", "bot_db_id"),
        ("idx_sp_user", "star_payments", "user_id"),
    ):
        if name not in {i["name"] for i in inspector.get_indexes(table)}:
            conn.execute(text(f"CREATE INDEX {name} ON {table}({cols})"))


def _count_triggers(conn):
    if conn.dialect.name == "sqlite":
        statements = [
            """CREATE TRIGGER IF NOT EXISTS file_count_insert AFTER INSERT ON file_mappings
               WHEN NEW.bot_db_id IS NOT NULL BEGIN
               INSERT INTO bot_file_counts(bot_db_id,count) VALUES(NEW.bot_db_id,1)
               ON CONFLICT(bot_db_id) DO UPDATE SET count=count+1; END""",
            """CREATE TRIGGER IF NOT EXISTS file_count_delete AFTER DELETE ON file_mappings
               WHEN OLD.bot_db_id IS NOT NULL BEGIN
               UPDATE bot_file_counts SET count=MAX(0,count-1) WHERE bot_db_id=OLD.bot_db_id; END""",
            """CREATE TRIGGER IF NOT EXISTS file_count_move AFTER UPDATE OF bot_db_id ON file_mappings
               WHEN OLD.bot_db_id IS NOT NEW.bot_db_id BEGIN
               UPDATE bot_file_counts SET count=MAX(0,count-1) WHERE bot_db_id=OLD.bot_db_id;
               INSERT INTO bot_file_counts(bot_db_id,count) SELECT NEW.bot_db_id,1 WHERE NEW.bot_db_id IS NOT NULL
               ON CONFLICT(bot_db_id) DO UPDATE SET count=count+1; END""",
        ]
    else:
        existing = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()"
                )
            )
        }
        statements = []
        for name, timing, body in (
            (
                "file_count_insert",
                "INSERT",
                """IF NEW.bot_db_id IS NOT NULL THEN
              INSERT INTO bot_file_counts(bot_db_id,count) VALUES(NEW.bot_db_id,1)
              ON DUPLICATE KEY UPDATE count=count+1; END IF;""",
            ),
            (
                "file_count_delete",
                "DELETE",
                """UPDATE bot_file_counts SET count=GREATEST(0,count-1)
              WHERE bot_db_id=OLD.bot_db_id;""",
            ),
            (
                "file_count_move",
                "UPDATE",
                """IF NOT (OLD.bot_db_id <=> NEW.bot_db_id) THEN
              UPDATE bot_file_counts SET count=GREATEST(0,count-1) WHERE bot_db_id=OLD.bot_db_id;
              IF NEW.bot_db_id IS NOT NULL THEN
              INSERT INTO bot_file_counts(bot_db_id,count) VALUES(NEW.bot_db_id,1)
              ON DUPLICATE KEY UPDATE count=count+1; END IF; END IF;""",
            ),
        ):
            if name not in existing:
                statements.append(
                    f"CREATE TRIGGER {name} AFTER {timing} ON file_mappings FOR EACH ROW BEGIN {body} END"
                )
    for statement in statements:
        conn.execute(text(statement))
