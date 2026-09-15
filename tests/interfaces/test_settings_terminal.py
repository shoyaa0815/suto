from textual.widgets import Input, OptionList, Static

from interfaces.settings_terminal import (
    ConfirmDiscardScreen,
    EditSettingScreen,
    SettingsApp,
)


async def test_arrow_and_enter_edit_then_save_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    app = SettingsApp(path)

    async with app.run_test(size=(90, 28)) as pilot:
        menu = app.query_one("#settings-options", OptionList)
        assert menu.highlighted == 0

        await pilot.press("enter")
        assert isinstance(app.screen, EditSettingScreen)
        field = app.screen.query_one("#edit-value", Input)
        field.value = "Asia/Bangkok"
        await pilot.press("enter")
        await pilot.pause()

        assert app.screen is app.screen_stack[0]
        assert app.settings.profile.timezone == "Asia/Bangkok"
        await pilot.press("down", "down", "down", "enter")
        await pilot.pause()

    content = path.read_text(encoding="utf-8")
    assert "timezone: Asia/Bangkok" in content
    assert app.saved is True


async def test_invalid_value_stays_in_editor_and_does_not_save(tmp_path):
    path = tmp_path / "config.yaml"
    app = SettingsApp(path)

    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.press("enter")
        field = app.screen.query_one("#edit-value", Input)
        field.value = "Mars/Olympus"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, EditSettingScreen)
        error = app.screen.query_one("#edit-error", Static).render().plain
        assert "unknown timezone" in error
        await pilot.press("escape")
        await pilot.press("down", "down", "down", "down", "enter")

    assert not path.exists()


async def test_exit_requires_confirmation_before_discarding_changes(tmp_path):
    path = tmp_path / "config.yaml"
    app = SettingsApp(path)

    async with app.run_test(size=(90, 28)) as pilot:
        await pilot.press("enter")
        field = app.screen.query_one("#edit-value", Input)
        field.value = "Asia/Bangkok"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down", "down", "down", "down", "enter")
        await pilot.pause()

        assert isinstance(app.screen, ConfirmDiscardScreen)
        await pilot.press("down", "enter")

    assert app.saved is False
    assert not path.exists()
