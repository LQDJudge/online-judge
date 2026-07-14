import difflib
import json

from django.apps import apps
from django.utils.translation import gettext as _

from judge.models import Profile


def get_raw_version_fields(version):
    try:
        data = json.loads(version.serialized_data)
        if data:
            return data[0].get("fields", {})
    except (json.JSONDecodeError, IndexError, KeyError):
        pass
    return {}


class RevisionDiffMixin:
    text_diff_fields = set()
    id_fields = {}
    profile_fields = set()
    skip_fields = {"id"}

    def _get_raw_fields(self, version):
        return get_raw_version_fields(version)

    def _compute_changes(self, old_dict, new_dict, model, name_cache=None):
        changes = []
        all_keys = set(old_dict.keys()) | set(new_dict.keys())
        name_cache = (
            name_cache if name_cache is not None else getattr(self, "_name_cache", {})
        )

        for key in sorted(all_keys):
            if key in self.skip_fields:
                continue
            old_val = old_dict.get(key)
            new_val = new_dict.get(key)
            if old_val == new_val:
                continue

            try:
                field = model._meta.get_field(key)
                label = str(field.verbose_name)
            except Exception:
                label = key

            change = {
                "field": label,
                "old": self._format_value(key, old_val, name_cache),
                "new": self._format_value(key, new_val, name_cache),
            }

            if key in self.text_diff_fields:
                old_text = str(old_val) if old_val else ""
                new_text = str(new_val) if new_val else ""
                change["old"] = old_text
                change["new"] = new_text
                diff_lines = list(
                    difflib.unified_diff(
                        old_text.splitlines(), new_text.splitlines(), lineterm="", n=2
                    )
                )
                change["diff_lines"] = [
                    line
                    for line in diff_lines
                    if not line.startswith("---") and not line.startswith("+++")
                ]

            changes.append(change)
        return changes

    def _build_name_cache(self, raw_fields_list, id_fields=None, profile_fields=None):
        id_fields = id_fields if id_fields is not None else self.id_fields
        profile_fields = (
            profile_fields if profile_fields is not None else self.profile_fields
        )
        ids_by_key = {}

        for fields in raw_fields_list:
            for key in id_fields:
                val = fields.get(key)
                if val is None:
                    continue
                ids_by_key.setdefault(key, set())
                if isinstance(val, list):
                    ids_by_key[key].update(val)
                else:
                    ids_by_key[key].add(val)

        all_profile_ids = set()
        for key in profile_fields:
            all_profile_ids.update(ids_by_key.get(key, set()))

        profile_name_map = {}
        if all_profile_ids:
            profiles = Profile.get_cached_instances(*all_profile_ids)
            profile_name_map = {profile.id: profile.username for profile in profiles}

        cache = {}
        for key, ids in ids_by_key.items():
            if not ids:
                continue
            if key in profile_fields:
                cache[key] = profile_name_map
                continue

            model_path, display_field = id_fields[key]
            app, model_name = model_path.split(".")
            model = apps.get_model(app, model_name)
            cache[key] = dict(
                model.objects.filter(id__in=ids).values_list("id", display_field)
            )
        return cache

    def _format_value(self, key, val, name_cache=None):
        if val is None:
            return ""

        name_cache = (
            name_cache if name_cache is not None else getattr(self, "_name_cache", {})
        )
        name_map = name_cache.get(key)

        if isinstance(val, list):
            if name_map:
                return ", ".join(name_map.get(item, str(item)) for item in val)
            return ", ".join(str(item) for item in val)

        if name_map and val:
            return name_map.get(val, str(val))

        val_str = str(val)
        if len(val_str) > 300:
            return val_str[:300] + "..."
        return val_str

    def _format_history_bool(self, value):
        return _("yes") if value in (True, "True", "true", "1", 1) else _("no")
