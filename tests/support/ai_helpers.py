class FakeClientSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def patch_model_chat(monkeypatch):
    """Route legacy chat fakes through the runtime model boundary."""
    from ai.execution import loop
    from ai.providers.model import ChatModelAdapter

    monkeypatch.setattr(loop, "build_model_router", ChatModelAdapter)
