"""Keyboard-driven terminal editor for host-managed Suto settings."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from application.configuration import (
    DEFAULT_CONFIG_PATH,
    load_settings,
    save_settings,
    update_profile_setting,
)


FIELD_LABELS = {
    "timezone": "Timezone",
    "locale": "Language",
    "display_name": "Display name",
}


class EditSettingScreen(ModalScreen[str | None]):
    """Edit and validate one setting without leaving the settings UI."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        key: str,
        value: str,
        validate: Callable[[str], None],
    ) -> None:
        super().__init__()
        self.key = key
        self.value = value
        self.validate = validate

    def compose(self) -> ComposeResult:
        with Container(id="edit-dialog"):
            yield Static(f"Edit {FIELD_LABELS[self.key]}", id="edit-title")
            yield Input(value=self.value, id="edit-value")
            yield Static("Enter confirm  •  Esc cancel", id="edit-help")
            yield Static("", id="edit-error")

    def on_mount(self) -> None:
        field = self.query_one("#edit-value", Input)
        field.focus()
        field.action_select_all()

    @on(Input.Submitted, "#edit-value")
    def submit_value(self) -> None:
        value = self.query_one("#edit-value", Input).value
        try:
            self.validate(value)
        except ValueError as error:
            self.query_one("#edit-error", Static).update(str(error))
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDiscardScreen(ModalScreen[bool]):
    """Require an explicit choice before discarding staged settings."""

    BINDINGS = [("escape", "keep_editing", "Keep editing")]

    def compose(self) -> ComposeResult:
        with Container(id="discard-dialog"):
            yield Static("Discard unsaved changes?", id="discard-title")
            yield OptionList(
                Option("Keep editing", id="keep"),
                Option("Discard and exit", id="discard"),
                id="discard-options",
            )

    @on(OptionList.OptionSelected, "#discard-options")
    def select_choice(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id == "discard")

    def action_keep_editing(self) -> None:
        self.dismiss(False)


class SettingsApp(App[None]):
    """Dedicated settings mode; it never starts AI, workers, or chat clients."""

    TITLE = "Suto Settings"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        ("escape", "request_exit", "Exit"),
        ("ctrl+q", "request_exit", "Exit"),
    ]

    CSS = """
    Screen {
        align: center middle;
        background: #000000;
        color: #e4e4e7;
    }

    #settings-shell {
        width: 72;
        height: auto;
        padding: 1 2;
        border: solid #3b82f6;
        background: #09090b;
    }

    #settings-title, #edit-title, #discard-title {
        height: 2;
        text-style: bold;
        color: #f4f4f5;
    }

    #settings-help, #edit-help {
        height: 2;
        color: #a1a1aa;
    }

    #settings-options, #discard-options {
        height: auto;
        background: #09090b;
        border: none;
    }

    #settings-status, #edit-error {
        height: 2;
        padding-top: 1;
        color: #f87171;
    }

    EditSettingScreen, ConfirmDiscardScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.75);
    }

    #edit-dialog, #discard-dialog {
        width: 64;
        height: auto;
        padding: 1 2;
        border: solid #3b82f6;
        background: #18181b;
    }

    #edit-value {
        background: #27272a;
        border: none;
    }
    """

    def __init__(self, path: str | Path = DEFAULT_CONFIG_PATH) -> None:
        super().__init__()
        self.path = Path(path)
        self.settings = load_settings(self.path)
        self.dirty = False
        self.saved = False

    def _options(self) -> list[Option]:
        profile = self.settings.profile
        return [
            Option(f"Timezone       {profile.timezone}", id="timezone"),
            Option(f"Language       {profile.locale}", id="locale"),
            Option(f"Display name   {profile.display_name}", id="display_name"),
            Option("Save", id="save"),
            Option("Exit", id="exit"),
        ]

    def compose(self) -> ComposeResult:
        with Container(id="settings-shell"):
            yield Static("Suto Settings", id="settings-title")
            yield Static(
                "↑/↓ select  •  Enter edit or choose  •  Esc exit",
                id="settings-help",
            )
            yield OptionList(*self._options(), id="settings-options")
            yield Static("", id="settings-status")

    def _validate_edit(self, key: str, value: str) -> None:
        update_profile_setting(self.settings, key, value)

    def _finish_edit(self, key: str, value: str | None) -> None:
        if value is None:
            return
        self.settings = update_profile_setting(self.settings, key, value)
        self.dirty = True
        menu = self.query_one("#settings-options", OptionList)
        menu.set_options(self._options())
        menu.highlighted = list(FIELD_LABELS).index(key)
        menu.focus()

    def _save_and_exit(self) -> None:
        status = self.query_one("#settings-status", Static)
        try:
            save_settings(self.settings, self.path)
        except (OSError, ValueError) as error:
            status.update(f"Save failed: {error}")
            return
        self.dirty = False
        self.saved = True
        self.exit()

    def _finish_discard(self, discard: bool | None) -> None:
        if discard:
            self.exit()
            return
        self.query_one("#settings-options", OptionList).focus()

    def _request_exit(self) -> None:
        if not self.dirty:
            self.exit()
            return
        self.push_screen(ConfirmDiscardScreen(), self._finish_discard)

    @on(OptionList.OptionSelected, "#settings-options")
    def select_option(self, event: OptionList.OptionSelected) -> None:
        key = event.option.id
        if key in FIELD_LABELS:
            value = getattr(self.settings.profile, key)
            self.push_screen(
                EditSettingScreen(
                    key,
                    value,
                    lambda candidate: self._validate_edit(key, candidate),
                ),
                lambda result: self._finish_edit(key, result),
            )
        elif key == "save":
            self._save_and_exit()
        elif key == "exit":
            self._request_exit()

    def action_request_exit(self) -> None:
        self._request_exit()


def run(mode: str) -> None:
    if mode != "settings":
        raise ValueError("the settings terminal only supports settings mode")
    app = SettingsApp()
    app.run()
    if app.saved:
        print(f"Saved {app.path}. Restart Suto to apply changes.")
