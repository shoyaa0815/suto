import pytest
from pathlib import Path

from tools.filesystem import (
    FilesystemOperations,
    file_list,
    file_read,
    file_write,
    list_files,
    read,
    write,
)


def test_filesystem_write_and_read(tmp_path):
    fs = FilesystemOperations(root_dir=tmp_path)
    file_path = tmp_path / "subdir" / "test.txt"

    res_write = fs.write(str(file_path), "Hello, Suto!")
    assert "Successfully wrote" in res_write
    assert file_path.read_text(encoding="utf-8") == "Hello, Suto!"

    content = fs.read(str(file_path))
    assert content == "Hello, Suto!"

    # Test append
    res_append = fs.write(str(file_path), " Added text", append=True)
    assert "Successfully appended" in res_append
    assert fs.read(str(file_path)) == "Hello, Suto! Added text"


def test_filesystem_read_slice(tmp_path):
    fs = FilesystemOperations(root_dir=tmp_path)
    file_path = tmp_path / "slice.txt"
    file_path.write_text("0123456789", encoding="utf-8")

    part = fs.read(str(file_path), offset=3, limit=4)
    assert part == "3456"


def test_filesystem_path_containment_violation(tmp_path):
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    fs = FilesystemOperations(root_dir=safe_root)

    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret", encoding="utf-8")

    # Reading outside safe root must be blocked
    result = fs.read(str(outside_file))
    assert "escapes allowed root directory" in result

    # Writing outside safe root must be blocked
    write_result = fs.write(str(outside_file), "overwrite")
    assert "escapes allowed root directory" in write_result


def test_filesystem_list(tmp_path):
    fs = FilesystemOperations(root_dir=tmp_path)
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b_dir").mkdir()
    (tmp_path / "b_dir" / "sub.txt").write_text("sub", encoding="utf-8")

    listing = fs.list(str(tmp_path))
    assert "a.txt" in listing
    assert "b_dir/" in listing

    rec_listing = fs.list(str(tmp_path), recursive=True)
    assert "a.txt" in rec_listing
    assert "b_dir/" in rec_listing
    assert "b_dir/sub.txt" in rec_listing


def test_filesystem_nonexistent_read_and_list(tmp_path):
    fs = FilesystemOperations(root_dir=tmp_path)
    assert "File not found" in fs.read(str(tmp_path / "none.txt"))
    assert "Directory not found" in fs.list(str(tmp_path / "no_dir"))


def test_filesystem_convenience_functions(tmp_path):
    test_file = tmp_path / "conv.txt"
    write(str(test_file), "hello from conv")
    assert read(str(test_file)) == "hello from conv"
    assert file_read(str(test_file)) == "hello from conv"
    file_write(str(test_file), "!")
    assert read(str(test_file)) == "!"
    assert "conv.txt" in list_files(str(tmp_path))
    assert "conv.txt" in file_list(str(tmp_path))
