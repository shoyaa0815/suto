"""Validated YAML editing with revision checks and existing atomic persistence."""

import difflib
import hashlib
from pathlib import Path

import yaml

from application.configuration import parse_settings, render_settings, save_settings


class SettingsConflict(Exception):
    pass


class SettingsEditor:
    def __init__(self, path: Path):
        self.path = path

    def snapshot(self):
        # Never expose an arbitrary existing file as raw YAML: first validate its
        # non-secret schema, then return the normalized, editable representation.
        if self.path.is_symlink():
            raise ValueError("Settings path must not be a symbolic link.")
        if self.path.exists() and self.path.stat().st_size > 32768:
            raise ValueError("Settings file exceeds 32 KiB.")
        raw = self.path.read_bytes() if self.path.exists() else None
        settings = self.parse(raw.decode("utf-8") if raw is not None else "")
        return {"yaml": render_settings(settings),
                "revision": hashlib.sha256(raw).hexdigest() if raw is not None else "missing"}

    @staticmethod
    def parse(source):
        if not isinstance(source, str) or len(source.encode("utf-8")) > 32768:
            raise ValueError("YAML must be text, no larger than 32 KiB.")
        try:
            return parse_settings(yaml.safe_load(source))
        except (yaml.YAMLError, ValueError, TypeError, RecursionError) as error:
            # Do not echo YAML values or arbitrary unknown keys into errors.
            raise ValueError("Invalid configuration. Check YAML syntax, version: 1, and profile timezone, locale and display_name.") from error

    def update(self, source, revision, *, save=False):
        settings = self.parse(source)
        before = self.snapshot()
        if revision != before["revision"]:
            raise SettingsConflict("config.yaml changed. Reload it before saving; your draft has been kept.")
        normalized = render_settings(settings)
        diff = "".join(difflib.unified_diff(
            before["yaml"].splitlines(keepends=True), normalized.splitlines(keepends=True),
            fromfile="Saved config.yaml", tofile="Your changes",
        ))
        if save:
            save_settings(settings, self.path)
            return {**self.snapshot(), "saved": True}
        return {"yaml": normalized, "diff": diff, "revision": revision}
