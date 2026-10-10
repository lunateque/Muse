from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS hls(id TEXT PRIMARY KEY, user TEXT NOT NULL, ids TEXT NOT NULL,
                at REAL NOT NULL, ended INT NOT NULL DEFAULT 0, created_at INT NOT NULL,
                updated_at INT NOT NULL)""",
)


def upgrade() -> None:
    for statement in SCHEMA:
        op.execute(statement)
