"""Terminal control for discrete Voice interactions."""

import asyncio

from prompt_toolkit import PromptSession

from application.configuration import load_settings

from .factory import VoiceSetupError, build_controller, diagnostics


async def run_console(controller, *, session=None, write=print) -> None:
    prompt = session or PromptSession()
    shown_approval = None

    def changed(state):
        nonlocal shown_approval
        if state.pending_approval_id and state.pending_approval_id != shown_approval:
            shown_approval = state.pending_approval_id
            write(f"Approval {shown_approval} for {state.tool_name or 'tool'}. "
                  f"Type /approve {shown_approval} or /deny {shown_approval}.")

    controller.on_change = changed
    write("Voice ready. /listen records one turn; /help lists controls.")
    try:
        while True:
            try:
                command = (await prompt.prompt_async("voice> ")).strip()
            except (EOFError, KeyboardInterrupt):
                return
            if command in {"/exit", "/quit"}:
                return
            if command == "/help":
                write("/listen · /session · /new · /resume ID · /skills · "
                      "/skill activate NAME · /skill deactivate NAME · /exit")
                continue
            if command == "/session":
                write(f"Session {controller.state.session_id}")
                continue
            if command == "/new":
                write(f"Session {controller.new_session()}")
                continue
            if command.startswith("/resume "):
                try:
                    controller.resume_session(command.split(maxsplit=1)[1])
                    write(f"Session {controller.state.session_id}")
                except ValueError:
                    write("Voice session unavailable")
                continue
            if command == "/skills":
                write("Selected Skills: " + (", ".join(controller.skills.names) or "none"))
                continue
            if command.startswith("/skill "):
                parts = command.split()
                try:
                    if len(parts) != 3:
                        raise ValueError("Invalid Skill command")
                    if parts[1] == "activate":
                        controller.activate_skill(parts[2])
                    elif parts[1] == "deactivate":
                        controller.deactivate_skill(parts[2])
                    else:
                        raise ValueError("Invalid Skill command")
                    write("Selected Skills: " + (", ".join(controller.skills.names) or "none"))
                except ValueError:
                    write("Unknown or inactive Skill")
                continue
            if command != "/listen":
                write("Unknown command. Type /help.")
                continue
            interaction = asyncio.create_task(controller.run_once())
            while not interaction.done():
                control = asyncio.create_task(prompt.prompt_async("voice control> "))
                done, _ = await asyncio.wait({interaction, control}, return_when=asyncio.FIRST_COMPLETED)
                if interaction in done:
                    control.cancel()
                    await asyncio.gather(control, return_exceptions=True)
                    break
                try:
                    action = control.result().strip()
                except (EOFError, KeyboardInterrupt):
                    action = "/cancel"
                if action == "/cancel":
                    await controller.cancel()
                elif action.startswith("/approve ") or action.startswith("/deny "):
                    operation, _, request_id = action.partition(" ")
                    choice = "allow_once" if operation == "/approve" else "deny"
                    write("Approval submitted" if controller.submit_approval(request_id, choice)
                          else "Approval unavailable or expired")
                else:
                    write("Use /cancel, /approve ID, or /deny ID during a turn.")
            try:
                turn = await interaction
            except asyncio.CancelledError:
                write("Voice interaction cancelled")
                continue
            if controller.state.transcript:
                write(f"You: {controller.state.transcript}")
            if controller.state.display_text:
                write(f"Suto: {controller.state.display_text}")
            if turn.status not in {"completed", "waiting_input"}:
                write(f"Voice status: {turn.status}")
    finally:
        await controller.close()


def run(mode: str = "agent") -> None:
    if mode != "agent":
        raise ValueError("Voice supports agent mode only")
    settings = load_settings()
    for line in diagnostics(settings):
        print(line, flush=True)
    if settings.voice.stt_provider == "openai" or settings.voice.tts_provider == "openai":
        print("Configured speech provider sends recorded audio and spoken text to OpenAI.", flush=True)
    try:
        controller = build_controller(settings)
    except VoiceSetupError as error:
        raise SystemExit(str(error)) from None
    asyncio.run(run_console(controller))
