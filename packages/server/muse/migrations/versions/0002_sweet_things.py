from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS secrets(id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL,
                recipient TEXT NOT NULL, track_id INT NOT NULL, text TEXT NOT NULL, created_at INT NOT NULL,
                found_at INT)""",
    """CREATE INDEX IF NOT EXISTS ix_secrets_recipient_track ON secrets(recipient, track_id, found_at)""",
    """CREATE TABLE IF NOT EXISTS pins(for_user TEXT NOT NULL, day TEXT NOT NULL, track_id INT NOT NULL,
                text TEXT NOT NULL DEFAULT '', set_by TEXT NOT NULL, set_at INT NOT NULL,
                PRIMARY KEY(for_user, day))""",
)


def upgrade() -> None:
    for statement in SCHEMA:
        op.execute(statement)
