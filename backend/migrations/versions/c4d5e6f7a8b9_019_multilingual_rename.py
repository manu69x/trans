"""Multilingua Tier 1 (2026-10-29): rename campi cablati EN→IT.

Multilingual Tier 1 (en→it/es/fr/de/pt): the DB columns that hardcoded the
Italian target are renamed to language-neutral names:

* ``translation_units.is_italian``           -> ``is_target_lang``
* ``translation_units.is_english``           -> ``is_source_lang``
* ``entities.italian_grammatical_gender``    -> ``target_grammatical_gender``
* ``glossary_terms.grammatical_gender_it``   -> ``grammatical_gender_target``

Values carry over unchanged (they are language-neutral scores/labels).

Revision ID: c4d5e6f7a8b9
Revises: b4c5d6e7f8a9
"""
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b4c5d6e7f8a9"
branch_labels = None
depends_on = None

_RENAMES = [
    ("translation_units", "is_italian", "is_target_lang"),
    ("translation_units", "is_english", "is_source_lang"),
    ("entities", "italian_grammatical_gender", "target_grammatical_gender"),
    ("glossary_terms", "grammatical_gender_it", "grammatical_gender_target"),
]


def upgrade() -> None:
    for table, old, new in _RENAMES:
        op.alter_column(table, old, new_column_name=new)


def downgrade() -> None:
    for table, old, new in reversed(_RENAMES):
        op.alter_column(table, new, new_column_name=old)
