from tools import (
    ALL_TOOL_SCHEMAS,
    ALL_TOOLS,
    file,
    filesystem,
    get_tools,
    git,
    python,
    terminal,
    web,
)


def test_facade_modules_exposed_cleanly():
    assert hasattr(terminal, "run")
    assert hasattr(file, "read")
    assert hasattr(file, "write")
    assert hasattr(file, "list_files")
    assert hasattr(git, "diff")
    assert hasattr(git, "status")
    assert hasattr(git, "log")
    assert hasattr(web, "search")
    assert hasattr(web, "fetch")
    assert hasattr(python, "run")


def test_domain_tools_registered_in_all_tools():
    expected_names = [
        "terminal.run",
        "terminal_run",
        "file.read",
        "file_read",
        "file.write",
        "file_write",
        "file.list",
        "file_list",
        "git.diff",
        "git_diff",
        "git.status",
        "git_status",
        "git.log",
        "git_log",
        "git.commit",
        "git_commit",
        "web.search",
        "web_search",
        "web.fetch",
        "web_fetch",
        "python.run",
        "python_run",
    ]
    for name in expected_names:
        assert name in ALL_TOOLS, f"{name} missing from ALL_TOOLS"
        assert name in ALL_TOOL_SCHEMAS, f"{name} missing from ALL_TOOL_SCHEMAS"
        schema = ALL_TOOL_SCHEMAS[name]
        assert schema["type"] == "function"
        assert schema["function"]["name"] == name
        assert "description" in schema["function"]
        assert "parameters" in schema["function"]


def test_get_tools_dispatches_cleanly(tmp_path):
    allowed = frozenset(["terminal.run", "file.write", "file.read", "python.run"])
    tools, schemas, guidance = get_tools(allowed)

    assert set(tools.keys()) == allowed
    assert len(schemas) == 4
    assert {s["function"]["name"] for s in schemas} == allowed

    # Test invoking through the tool dispatch map (as AI model loop does)
    test_file = tmp_path / "hello.txt"
    write_res = tools["file.write"](path=str(test_file), content="content from json dispatch")
    assert "Successfully wrote" in write_res

    read_res = tools["file.read"](path=str(test_file))
    assert read_res == "content from json dispatch"

    term_res = tools["terminal.run"](command="echo 'from terminal dispatch'")
    assert "STDOUT:\nfrom terminal dispatch" in term_res

    py_res = tools["python.run"](code="print(7 * 6)")
    assert "STDOUT:\n42" in py_res
