from sqlalchemy import inspect, text
from database import Base, engine


def _add_missing_columns():
    """Add columns introduced after initial table creation (SQLite/Postgres)."""
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()

    existing_user_cols = {col["name"] for col in inspector.get_columns("users")}
    with engine.begin() as conn:
        if "totp_secret" not in existing_user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN totp_secret VARCHAR(32)"))
        if "totp_enabled" not in existing_user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN DEFAULT FALSE"))

        existing_q_cols = {col["name"] for col in inspector.get_columns("questions")}
        if "parse_draft_id" not in existing_q_cols:
            conn.execute(text("ALTER TABLE questions ADD COLUMN parse_draft_id VARCHAR(36)"))
        if "archived" not in existing_q_cols:
            conn.execute(text("ALTER TABLE questions ADD COLUMN archived BOOLEAN DEFAULT FALSE"))
        if "question_type" not in existing_q_cols:
            conn.execute(text("ALTER TABLE questions ADD COLUMN question_type VARCHAR(20) DEFAULT 'mcq'"))
        if "source_key" not in existing_q_cols:
            conn.execute(text("ALTER TABLE questions ADD COLUMN source_key VARCHAR(40)"))
        if "pool_question_id" not in existing_q_cols:
            conn.execute(text("ALTER TABLE questions ADD COLUMN pool_question_id INTEGER"))

        existing_test_cols = {col["name"] for col in inspector.get_columns("tests")}
        if "is_published" not in existing_test_cols:
            conn.execute(text("ALTER TABLE tests ADD COLUMN is_published BOOLEAN DEFAULT TRUE"))

        # Widen choice columns from VARCHAR(500) to TEXT
        for col in ("choice_a", "choice_b", "choice_c", "choice_d"):
            conn.execute(text(f"ALTER TABLE questions ALTER COLUMN {col} TYPE TEXT"))

        if "parse_drafts" in existing_tables:
            existing_draft_cols = {col["name"] for col in inspector.get_columns("parse_drafts")}
            if "notes" not in existing_draft_cols:
                conn.execute(text("ALTER TABLE parse_drafts ADD COLUMN notes TEXT"))
            if "error_text" not in existing_draft_cols:
                conn.execute(text("ALTER TABLE parse_drafts ADD COLUMN error_text TEXT"))
            if "file_blob" not in existing_draft_cols:
                conn.execute(text("ALTER TABLE parse_drafts ADD COLUMN file_blob BYTEA"))

        if "parse_drafts" not in existing_tables:
            conn.execute(text("""
                CREATE TABLE parse_drafts (
                    id VARCHAR(36) PRIMARY KEY,
                    file_hash VARCHAR(64) NOT NULL,
                    original_filename VARCHAR(500),
                    status VARCHAR(20) DEFAULT 'pending_review',
                    result_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """))
            conn.execute(text("CREATE INDEX ix_parse_drafts_file_hash ON parse_drafts (file_hash)"))

        # IRT tables — created by Base.metadata.create_all; add missing columns if tables
        # already exist from a partial migration.
        if "item_param_versions" in existing_tables:
            existing_ipv_cols = {col["name"] for col in inspector.get_columns("item_param_versions")}
            if "created_at" not in existing_ipv_cols:
                conn.execute(text("ALTER TABLE item_param_versions ADD COLUMN created_at TIMESTAMP"))

        if "item_params" in existing_tables:
            existing_ip_cols = {col["name"] for col in inspector.get_columns("item_params")}
            if "updated_at" not in existing_ip_cols:
                conn.execute(text("ALTER TABLE item_params ADD COLUMN updated_at TIMESTAMP"))
            if "version_id" not in existing_ip_cols:
                conn.execute(text("ALTER TABLE item_params ADD COLUMN version_id INTEGER"))
            if "n_responses" not in existing_ip_cols:
                conn.execute(text("ALTER TABLE item_params ADD COLUMN n_responses INTEGER"))

        if "test_scores" in existing_tables:
            existing_ts_cols = {col["name"] for col in inspector.get_columns("test_scores")}
            for col_def in (
                ("rw_band", "VARCHAR(30)"),
                ("math_band", "VARCHAR(30)"),
                ("rw_theta", "FLOAT"),
                ("math_theta", "FLOAT"),
                ("rw_route", "VARCHAR(10)"),
                ("math_route", "VARCHAR(10)"),
                ("param_version", "VARCHAR(100)"),
                ("computed_at", "TIMESTAMP"),
            ):
                if col_def[0] not in existing_ts_cols:
                    conn.execute(text(f"ALTER TABLE test_scores ADD COLUMN {col_def[0]} {col_def[1]}"))


def init_db():
    """Create all database tables defined in models.py."""
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


if __name__ == "__main__":
    init_db()
    print("Database initialized: sat.db")
