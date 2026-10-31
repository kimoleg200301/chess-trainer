from logging.config import fileConfig

from alembic import context

from app.config import settings
from app.db import engine
from app.models import Base

config = context.config
fileConfig(config.config_file_name)
target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(url=settings().database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with engine().connect() as connection:
        # PostgreSQL lock serializes concurrent first starts/migrations.
        connection.exec_driver_sql("SELECT pg_advisory_lock(471938212)")
        connection.commit()
        try:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.exec_driver_sql("SELECT pg_advisory_unlock(471938212)")
            connection.commit()
