"""MariaDB migration operations for non-blocking large-table chat DDL."""

from contextlib import contextmanager

from django.db import migrations


def disable_mariadb_statement_timeout(apps, schema_editor):
    """Temporarily remove the statement cap while retaining a short lock wait."""
    connection = schema_editor.connection
    if connection.vendor != "mysql" or not getattr(
        connection, "mysql_is_mariadb", False
    ):
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SET @chat_box_old_max_statement_time = @@SESSION.max_statement_time"
        )
        cursor.execute("SET SESSION max_statement_time = 0")
        cursor.execute(
            "SET @chat_box_old_lock_wait_timeout = @@SESSION.lock_wait_timeout"
        )
        cursor.execute("SET SESSION lock_wait_timeout = 5")


def restore_mariadb_statement_timeout(apps, schema_editor):
    """Restore the session limits saved by disable_mariadb_statement_timeout."""
    connection = schema_editor.connection
    if connection.vendor != "mysql" or not getattr(
        connection, "mysql_is_mariadb", False
    ):
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SET SESSION max_statement_time = "
            "COALESCE(@chat_box_old_max_statement_time, @@GLOBAL.max_statement_time)"
        )
        cursor.execute("SET @chat_box_old_max_statement_time = NULL")
        cursor.execute(
            "SET SESSION lock_wait_timeout = "
            "COALESCE(@chat_box_old_lock_wait_timeout, @@GLOBAL.lock_wait_timeout)"
        )
        cursor.execute("SET @chat_box_old_lock_wait_timeout = NULL")


@contextmanager
def online_mariadb_templates(schema_editor, replacements):
    connection = schema_editor.connection
    if connection.vendor != "mysql" or not getattr(
        connection, "mysql_is_mariadb", False
    ):
        yield
        return

    originals = {name: getattr(schema_editor, name) for name in replacements}
    try:
        for name, template in replacements.items():
            setattr(schema_editor, name, template)
        yield
    finally:
        for name, template in originals.items():
            setattr(schema_editor, name, template)


class OnlineMariaDBAddField(migrations.AddField):
    """Require LOCK=NONE for a known-safe, non-relational Message column."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        with online_mariadb_templates(
            schema_editor,
            {
                "sql_create_column": (
                    "ALTER ONLINE TABLE %(table)s ADD COLUMN "
                    "%(column)s %(definition)s"
                )
            },
        ):
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        with online_mariadb_templates(
            schema_editor,
            {
                "sql_delete_column": (
                    "ALTER ONLINE TABLE %(table)s DROP COLUMN %(column)s"
                )
            },
        ):
            super().database_backwards(app_label, schema_editor, from_state, to_state)


class OnlineMariaDBAddIndex(migrations.AddIndex):
    """Require LOCK=NONE for an index on the large Message table."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        with online_mariadb_templates(
            schema_editor,
            {
                "sql_create_index": (
                    "ALTER ONLINE TABLE %(table)s ADD INDEX %(name)s "
                    "(%(columns)s)%(extra)s"
                )
            },
        ):
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        with online_mariadb_templates(
            schema_editor,
            {"sql_delete_index": ("ALTER ONLINE TABLE %(table)s DROP INDEX %(name)s")},
        ):
            super().database_backwards(app_label, schema_editor, from_state, to_state)


class OnlineMariaDBAlterForeignKeyField(migrations.AlterField):
    """Alter a large-table FK without permitting a blocking DDL fallback."""

    @contextmanager
    def _online_alter(self, schema_editor):
        connection = schema_editor.connection
        is_mariadb = connection.vendor == "mysql" and getattr(
            connection, "mysql_is_mariadb", False
        )
        if not is_mariadb:
            yield
            return

        with connection.cursor() as cursor:
            cursor.execute("SELECT @@SESSION.foreign_key_checks")
            old_foreign_key_checks = cursor.fetchone()[0]
            cursor.execute("SET SESSION foreign_key_checks = 0")
        try:
            with online_mariadb_templates(
                schema_editor,
                {
                    "sql_alter_column": "ALTER ONLINE TABLE %(table)s %(changes)s",
                    "sql_create_fk": (
                        "ALTER ONLINE TABLE %(table)s ADD CONSTRAINT %(name)s "
                        "FOREIGN KEY (%(column)s) REFERENCES %(to_table)s "
                        "(%(to_column)s)%(deferrable)s"
                    ),
                    "sql_delete_fk": (
                        "ALTER ONLINE TABLE %(table)s DROP FOREIGN KEY %(name)s"
                    ),
                },
            ):
                yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SET SESSION foreign_key_checks = %s", [old_foreign_key_checks]
                )

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        with self._online_alter(schema_editor):
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        with self._online_alter(schema_editor):
            super().database_backwards(app_label, schema_editor, from_state, to_state)
