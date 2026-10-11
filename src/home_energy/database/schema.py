"""Shared schema ownership and transactional repository connections."""

from contextlib import contextmanager
from importlib.resources import files

from psycopg import sql

from .connection import connect


SCHEMA_NAME = "home_energy"


class Repository:
    def __init__(self, connection_factory=connect):
        self.connection_factory = connection_factory

    @contextmanager
    def _connection(self):
        with self.connection_factory() as connection:
            connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(SCHEMA_NAME)))
            yield connection

    def initialize_schema(self) -> None:
        schema = files("home_energy.database").joinpath("schema.sql").read_text(encoding="utf-8")
        with self._connection() as connection:
            connection.execute(sql.SQL(schema).format(schema=sql.Identifier(SCHEMA_NAME)))


def initialize_schema(connection_factory=connect) -> None:
    Repository(connection_factory).initialize_schema()
