from alembic import context
from sqlalchemy import engine_from_config, pool

from app.models import Base, UtcDateTime

config = context.config
target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    # Write the plain SQLAlchemy type, so a migration never imports app code that may change.
    if type_ == "type" and isinstance(obj, UtcDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations() -> None:
    # A plain engine on purpose: batch migrations on SQLite must run with foreign keys off.
    engine = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
