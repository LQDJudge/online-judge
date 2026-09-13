"""Search decoded choice text in the site's MariaDB JSON fields."""

from django.db.models import F, Func, Q, TextField, Value
from django.db.models.functions import Collate
from django.db.models.lookups import IsNull


def choice_text_search(term):
    # JSON_SEARCH uses LIKE patterns. Escape literal wildcard/escape characters.
    pattern = "%" + term.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
    match_path = Func(
        Collate(F("choices"), "utf8mb4_unicode_ci"),
        Value("one"),
        Collate(Value(pattern), "utf8mb4_unicode_ci"),
        Value("!"),
        Value("$[*].text"),
        function="JSON_SEARCH",
        output_field=TextField(),
    )
    # Searching the serialized array with icontains misses JSON-escaped Unicode.
    return Q(IsNull(match_path, False))
