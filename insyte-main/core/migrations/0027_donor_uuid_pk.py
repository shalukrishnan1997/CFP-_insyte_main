"""Migrate Donor primary key from URN (varchar) to UUID.

Changes:
- Donor.id: new UUIDField primary key
- Donor.urn: changed from primary_key to a transitional nullable unique CharField
- All FK columns that referenced core_donor.urn are migrated to reference
  core_donor.id (UUID).

Affected tables:
  core_donation          donor_id
  core_datafiledonor     house_file_donor_id
  core_scanplaceholder   matched_donor_id
  core_stripecustomer    donor_id
"""

import uuid

from django.db import migrations, models


def _column_exists(schema_editor: object, table_name: str, column_name: str) -> bool:
    """Return whether the given column exists on the current database backend."""
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        columns = connection.introspection.get_table_description(cursor, table_name)
    return any(column.name == column_name for column in columns)


def _update_uuid_fk_column(
    schema_editor: object,
    table_name: str,
    target_column: str,
    temp_column: str,
) -> None:
    """Copy temporary UUID values into the final FK column."""
    cast_suffix = "::uuid" if schema_editor.connection.vendor == "postgresql" else ""
    schema_editor.execute(
        f"UPDATE {table_name} SET {target_column} = {temp_column}{cast_suffix} "
        f"WHERE {temp_column} IS NOT NULL"
    )


def _drop_column(schema_editor: object, table_name: str, column_name: str) -> None:
    """Drop a column, cleaning up SQLite indexes first when required."""
    if not _column_exists(schema_editor, table_name, column_name):
        return

    if schema_editor.connection.vendor == "sqlite":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f'PRAGMA index_list("{table_name}")')
            indexes = cursor.fetchall()
            for _, index_name, *_ in indexes:
                cursor.execute(f'PRAGMA index_info("{index_name}")')
                if any(index_column[2] == column_name for index_column in cursor.fetchall()):
                    schema_editor.execute(f'DROP INDEX "{index_name}"')

    schema_editor.execute(f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"')


# ---------------------------------------------------------------------------
# RunPython helpers
# ---------------------------------------------------------------------------


def populate_donor_uuids(apps: object, schema_editor: object) -> None:
    """Assign a UUID to every Donor row via the ORM.

    Args:
        apps: Django app registry.
        schema_editor: Database schema editor (unused; ORM used instead).
    """
    import uuid as uuid_module

    Donor = apps.get_model("core", "Donor")
    # Only populate donors that don't have an ID yet
    for donor in Donor.objects.filter(id__isnull=True):
        donor.id = uuid_module.uuid4()
        donor.save(update_fields=["id"])


def copy_donor_fks_to_uuid(apps: object, schema_editor: object) -> None:
    """Copy all URN-based donor FKs to new UUID temp columns.

    Uses string-formatted SQL (safe: UUIDs are predictable hex strings,
    row IDs are UUIDs — no injection risk).

    Args:
        apps: Django app registry.
        schema_editor: Database schema editor.
    """

    conn = schema_editor.connection
    with conn.cursor() as cursor:
        # Build urn → uuid mapping
        cursor.execute("SELECT urn, id FROM core_donor")
        urn_to_uuid: dict[str, str] = {}
        for urn, uid in cursor.fetchall():
            # Django stores UUIDs without hyphens; strip them for safety
            urn_to_uuid[urn] = str(uid).replace("-", "")

    # Use separate cursor context per table to avoid long-held locks
    with conn.cursor() as cursor:
        # core_donation.donor_id (varchar urn) → donor_new_id
        if _column_exists(schema_editor, "core_donation", "donor_id"):
            cursor.execute(
                "SELECT id, donor_id FROM core_donation WHERE donor_id IS NOT NULL"
            )
            rows = cursor.fetchall()
            for row_id, urn in rows:
                new_uuid = urn_to_uuid.get(str(urn))
                if new_uuid:
                    rid = str(row_id).replace("-", "")
                    cursor.execute(
                        f"UPDATE core_donation SET donor_new_id = '{new_uuid}'"
                        f" WHERE id = '{rid}'"
                    )

        # core_datafiledonor.house_file_donor_id → house_file_donor_new_id
        if _column_exists(schema_editor, "core_datafiledonor", "house_file_donor_id"):
            cursor.execute(
                "SELECT id, house_file_donor_id FROM core_datafiledonor"
                " WHERE house_file_donor_id IS NOT NULL"
            )
            rows = cursor.fetchall()
            for row_id, urn in rows:
                new_uuid = urn_to_uuid.get(str(urn))
                if new_uuid:
                    rid = str(row_id).replace("-", "")
                    cursor.execute(
                        f"UPDATE core_datafiledonor"
                        f" SET house_file_donor_new_id = '{new_uuid}'"
                        f" WHERE id = '{rid}'"
                    )

        # core_scanplaceholder.matched_donor_id → matched_donor_new_id
        if _column_exists(schema_editor, "core_scanplaceholder", "matched_donor_id"):
            cursor.execute(
                "SELECT id, matched_donor_id FROM core_scanplaceholder"
                " WHERE matched_donor_id IS NOT NULL"
            )
            rows = cursor.fetchall()
            for row_id, urn in rows:
                new_uuid = urn_to_uuid.get(str(urn))
                if new_uuid:
                    rid = str(row_id).replace("-", "")
                    cursor.execute(
                        f"UPDATE core_scanplaceholder"
                        f" SET matched_donor_new_id = '{new_uuid}'"
                        f" WHERE id = '{rid}'"
                    )

        # core_stripecustomer.donor_id → donor_new_id
        if _column_exists(schema_editor, "core_stripecustomer", "donor_id"):
            cursor.execute(
                "SELECT id, donor_id FROM core_stripecustomer WHERE donor_id IS NOT NULL"
            )
            rows = cursor.fetchall()
            for row_id, urn in rows:
                new_uuid = urn_to_uuid.get(str(urn))
                if new_uuid:
                    rid = str(row_id).replace("-", "")
                    cursor.execute(
                        f"UPDATE core_stripecustomer"
                        f" SET donor_new_id = '{new_uuid}'"
                        f" WHERE id = '{rid}'"
                    )


# Raw SQL to recreate core_donor with the schema that exists at migration 0027.
# Later migrations add client-scoped uniqueness and newer donor fields, so this
# table definition must stay aligned with the 0027 project state rather than the
# current runtime model.
# Executed AFTER the old varchar FK columns have been removed from all related
# tables (so no FK constraints point at core_donor.urn at that point).
_RECREATE_DONOR_TABLE = [
    'ALTER TABLE "core_donor" RENAME TO "core_donor_old"',
    """CREATE TABLE "core_donor" (
        "id"                 uuid         NOT NULL PRIMARY KEY,
        "urn"                varchar(50)  NULL     UNIQUE,
        "title"              varchar(10)  NOT NULL DEFAULT '',
        "first_name"         varchar(100) NOT NULL,
        "last_name"          varchar(100) NOT NULL,
        "email"              varchar(254) NOT NULL DEFAULT '',
        "phone"              varchar(20)  NOT NULL DEFAULT '',
        "address_line1"      varchar(255) NOT NULL DEFAULT '',
        "address_line2"      varchar(255) NOT NULL DEFAULT '',
        "city"               varchar(100) NOT NULL DEFAULT '',
        "county"             varchar(100) NOT NULL DEFAULT '',
        "postcode"           varchar(20)  NOT NULL DEFAULT '',
        "country"            varchar(100) NOT NULL DEFAULT 'United Kingdom',
        "date_of_birth"      date         NULL,
        "age"                integer NULL CHECK ("age" >= 0),
        "consent_contact"    bool         NOT NULL,
        "opt_in_email"       bool         NOT NULL,
        "opt_in_sms"         bool         NOT NULL,
        "opt_in_phone"       bool         NOT NULL,
        "opt_in_post"        bool         NOT NULL,
        "created_at"         timestamp with time zone NOT NULL,
        "updated_at"         timestamp with time zone NOT NULL,
        "created_by_id"      uuid     NULL
            REFERENCES "core_user" ("id") DEFERRABLE INITIALLY DEFERRED,
        "verification_status" varchar(20) NOT NULL DEFAULT 'verified'
    )""",
    """
    INSERT INTO "core_donor" (
        id, urn, title, first_name, last_name, email, phone,
        address_line1, address_line2, city, county, postcode, country,
        date_of_birth, age, consent_contact, opt_in_email, opt_in_sms,
        opt_in_phone, opt_in_post, created_at, updated_at,
        created_by_id, verification_status
    )
    SELECT
        id, urn, title, first_name, last_name, email, phone,
        address_line1, address_line2, city, county, postcode, country,
        date_of_birth, age, consent_contact, opt_in_email, opt_in_sms,
        opt_in_phone, opt_in_post, created_at, updated_at,
        created_by_id, verification_status
    FROM "core_donor_old"
    """,
    'DROP TABLE "core_donor_old"',
    'CREATE INDEX "core_donor_urn_idx" ON "core_donor" ("urn")',
    'CREATE INDEX "core_donor_name_idx" ON "core_donor" ("last_name", "first_name")',
    'CREATE INDEX "core_donor_email_idx" ON "core_donor" ("email")',
    'CREATE INDEX "core_donor_postcode_idx" ON "core_donor" ("postcode")',
    'CREATE INDEX "core_donor_verification_idx" ON "core_donor" ("verification_status")',
]


def populate_donation_uuid_fk(apps: object, schema_editor: object) -> None:
    """Populate donation donor FK from the temporary UUID column."""
    _update_uuid_fk_column(schema_editor, "core_donation", "donor_id", "donor_new_id")


def populate_datafiledonor_uuid_fk(apps: object, schema_editor: object) -> None:
    """Populate data-file donor FK from the temporary UUID column."""
    _update_uuid_fk_column(
        schema_editor,
        "core_datafiledonor",
        "house_file_donor_id",
        "house_file_donor_new_id",
    )


def populate_scanplaceholder_uuid_fk(apps: object, schema_editor: object) -> None:
    """Populate scan placeholder donor FK from the temporary UUID column."""
    _update_uuid_fk_column(
        schema_editor,
        "core_scanplaceholder",
        "matched_donor_id",
        "matched_donor_new_id",
    )


def populate_stripecustomer_uuid_fk(apps: object, schema_editor: object) -> None:
    """Populate Stripe customer donor FK from the temporary UUID column."""
    _update_uuid_fk_column(
        schema_editor, "core_stripecustomer", "donor_id", "donor_new_id"
    )


def drop_donation_legacy_fk(apps: object, schema_editor: object) -> None:
    """Drop the legacy donation donor FK column safely."""
    _drop_column(schema_editor, "core_donation", "donor_id")


def drop_datafiledonor_legacy_fk(apps: object, schema_editor: object) -> None:
    """Drop the legacy data-file donor FK column safely."""
    _drop_column(schema_editor, "core_datafiledonor", "house_file_donor_id")


def drop_scanplaceholder_legacy_fk(apps: object, schema_editor: object) -> None:
    """Drop the legacy scan placeholder donor FK column safely."""
    _drop_column(schema_editor, "core_scanplaceholder", "matched_donor_id")


def drop_stripecustomer_legacy_fk(apps: object, schema_editor: object) -> None:
    """Drop the legacy Stripe customer donor FK column safely."""
    _drop_column(schema_editor, "core_stripecustomer", "donor_id")


class Migration(migrations.Migration):
    """Migrate Donor from URN primary key to UUID primary key."""
    
    # 🔥 CRITICAL FIX — prevents PostgreSQL pending trigger error
    atomic = False

    dependencies = [
        ("core", "0026_remove_deprecated_donation_fields"),
    ]

    operations = [
        # ------------------------------------------------------------------ #
        # Phase 1: Add UUID id column to Donor (nullable initially)
        # ------------------------------------------------------------------ #
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    'ALTER TABLE "core_donor" ADD COLUMN "id" uuid NULL',
                    reverse_sql='ALTER TABLE "core_donor" DROP COLUMN "id"',
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="donor",
                    name="id",
                    field=models.UUIDField(
                        editable=False,
                        null=True,
                        help_text="System-generated unique identifier for the donor",
                    ),
                ),
            ],
        ),
        migrations.RunPython(
            populate_donor_uuids,
            reverse_code=migrations.RunPython.noop,
        ),
        # ------------------------------------------------------------------ #
        # Phase 2: Add temporary UUID columns to all FK tables (raw fields,
        #          no DB constraints yet — constraints come back later)
        # ------------------------------------------------------------------ #
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    'ALTER TABLE "core_donation" ADD COLUMN "donor_new_id" varchar(32) NULL',
                    reverse_sql='ALTER TABLE "core_donation" DROP COLUMN "donor_new_id"',
                ),
                migrations.RunSQL(
                    'ALTER TABLE "core_datafiledonor" ADD COLUMN "house_file_donor_new_id" varchar(32) NULL',
                    reverse_sql='ALTER TABLE "core_datafiledonor" DROP COLUMN "house_file_donor_new_id"',
                ),
                migrations.RunSQL(
                    'ALTER TABLE "core_scanplaceholder" ADD COLUMN "matched_donor_new_id" varchar(32) NULL',
                    reverse_sql='ALTER TABLE "core_scanplaceholder" DROP COLUMN "matched_donor_new_id"',
                ),
                migrations.RunSQL(
                    'ALTER TABLE "core_stripecustomer" ADD COLUMN "donor_new_id" varchar(32) NULL',
                    reverse_sql='ALTER TABLE "core_stripecustomer" DROP COLUMN "donor_new_id"',
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="donation",
                    name="donor_new_id",
                    field=models.CharField(
                        max_length=32,
                        null=True,
                        db_index=True,
                        help_text="Temporary UUID FK migration column",
                    ),
                ),
                migrations.AddField(
                    model_name="datafiledonor",
                    name="house_file_donor_new_id",
                    field=models.CharField(
                        max_length=32,
                        null=True,
                        db_index=True,
                        help_text="Temporary UUID FK migration column",
                    ),
                ),
                migrations.AddField(
                    model_name="scanplaceholder",
                    name="matched_donor_new_id",
                    field=models.CharField(
                        max_length=32,
                        null=True,
                        db_index=True,
                        help_text="Temporary UUID FK migration column",
                    ),
                ),
                migrations.AddField(
                    model_name="stripecustomer",
                    name="donor_new_id",
                    field=models.CharField(
                        max_length=32,
                        null=True,
                        db_index=True,
                        help_text="Temporary UUID FK migration column",
                    ),
                ),
            ],
        ),
        # ------------------------------------------------------------------ #
        # Phase 3: Copy URN-based FK values to UUID temp columns
        # ------------------------------------------------------------------ #
        migrations.RunPython(
            copy_donor_fks_to_uuid,
            reverse_code=migrations.RunPython.noop,
        ),
        # ------------------------------------------------------------------ #
        # Phase 4: Drop old varchar FK columns (urn-based)
        #          This removes all FK constraints pointing at core_donor.urn
        # ------------------------------------------------------------------ #
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    drop_donation_legacy_fk,
                    reverse_code=migrations.RunPython.noop,
                ),
                migrations.RunPython(
                    drop_datafiledonor_legacy_fk,
                    reverse_code=migrations.RunPython.noop,
                ),
                migrations.RunPython(
                    drop_scanplaceholder_legacy_fk,
                    reverse_code=migrations.RunPython.noop,
                ),
                migrations.RunPython(
                    drop_stripecustomer_legacy_fk,
                    reverse_code=migrations.RunPython.noop,
                ),
            ],
            state_operations=[
                migrations.RemoveField(model_name="donation", name="donor"),
                migrations.RemoveField(
                    model_name="datafiledonor", name="house_file_donor"
                ),
                migrations.RemoveField(
                    model_name="scanplaceholder", name="matched_donor"
                ),
                migrations.RemoveField(model_name="stripecustomer", name="donor"),
            ],
        ),
        # ------------------------------------------------------------------ #
        # Phase 5: Recreate core_donor with id as primary key, urn nullable
        #          unique.  No FK constraints point here now, so safe on SQLite.
        # ------------------------------------------------------------------ #
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(sql, reverse_sql=migrations.RunSQL.noop)
                for sql in _RECREATE_DONOR_TABLE
            ],
            state_operations=[
                # Declare urn as a regular nullable unique CharField
                migrations.AlterField(
                    model_name="donor",
                    name="urn",
                    field=models.CharField(
                        blank=True,
                        help_text=(
                            "Unique Reference Number from the charity. "
                            "Null for new donors captured before the charity "
                            "assigns a URN."
                        ),
                        max_length=50,
                        null=True,
                        unique=True,
                    ),
                ),
                # Declare id as the new primary key
                migrations.AlterField(
                    model_name="donor",
                    name="id",
                    field=models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        help_text="System-generated unique identifier for the donor",
                    ),
                ),
            ],
        ),
        # ------------------------------------------------------------------ #
        # Phase 6: Re-add proper FK columns (now UUID-based, referencing
        #          core_donor.id).  Populate from the temp columns via RunSQL.
        # ------------------------------------------------------------------ #

        # core_donation.donor
        migrations.AddField(
            model_name="donation",
            name="donor",
            field=models.ForeignKey(
                blank=True,
                help_text="Donor from house file (main donor list)",
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="donations",
                to="core.donor",
            ),
        ),
        migrations.RunPython(
            populate_donation_uuid_fk,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveField(model_name="donation", name="donor_new_id"),

        # core_datafiledonor.house_file_donor
        migrations.AddField(
            model_name="datafiledonor",
            name="house_file_donor",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional link to global house file Donor record",
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="data_file_entries",
                to="core.donor",
            ),
        ),
        migrations.RunPython(
            populate_datafiledonor_uuid_fk,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name="datafiledonor", name="house_file_donor_new_id"
        ),

        # core_scanplaceholder.matched_donor
        migrations.AddField(
            model_name="scanplaceholder",
            name="matched_donor",
            field=models.ForeignKey(
                blank=True,
                help_text="Matched house file donor",
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="scan_placeholders",
                to="core.donor",
            ),
        ),
        migrations.RunPython(
            populate_scanplaceholder_uuid_fk,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name="scanplaceholder", name="matched_donor_new_id"
        ),

        # core_stripecustomer.donor
        migrations.AddField(
            model_name="stripecustomer",
            name="donor",
            field=models.ForeignKey(
                blank=True,
                help_text="Linked donor for donation payments",
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="stripe_customers",
                to="core.donor",
            ),
        ),
        migrations.RunPython(
            populate_stripecustomer_uuid_fk,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveField(model_name="stripecustomer", name="donor_new_id"),
    ]
