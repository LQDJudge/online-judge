"""Shared review context and serialization helpers."""

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import models


class ReviewInputError(Exception):
    """Required current inputs could not be loaded safely."""


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return str(value)
    return value


def canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest_json(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _normalized_text(value):
    # Preserve significant indentation and spaces; normalize only newline encoding.
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def _scalars(instance, excluded=()):
    return {
        field.attname: _json_value(getattr(instance, field.attname))
        for field in instance._meta.concrete_fields
        if field.name not in excluded and not isinstance(field, models.FileField)
    }
