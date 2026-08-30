import pytest

from tools.file_reader import (
    DocumentIndex,
    SEARCH_SCHEMA,
    SUMMARY_SCHEMA,
    SourceSection,
    build_attachment_tools,
    build_read_attached_file,
    chunk_sections,
    clear_document_cache,
    estimate_tokens,
    extract_text,
    get_document,
    retrieve_chunks,
    summarize_document,
)


@pytest.fixture(autouse=True)
def _empty_document_cache():
    clear_document_cache()
    yield
    clear_document_cache()


def test_extracts_utf8_text():
    assert extract_text("notes.txt", "สวัสดี".encode()) == "สวัสดี"


def test_token_estimate_handles_latin_and_thai():
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("ภาษาไทย") == 7


def test_chunking_respects_token_budget_and_source_metadata():
    text = "\n\n".join(f"paragraph {number} has useful facts" for number in range(40))

    chunks = chunk_sections(
        [SourceSection("page 7", text)],
        target_tokens=35,
        overlap_tokens=10,
    )

    assert len(chunks) > 1
    assert all(chunk.token_count <= 35 for chunk in chunks)
    assert all(chunk.sources == ("page 7",) for chunk in chunks)


def test_document_cache_reuses_extraction_and_index():
    first = get_document("notes.txt", b"cached contents")
    second = get_document("notes.txt", b"cached contents")

    assert first is second


def test_bound_reader_only_reads_known_small_attachment():
    read_attached_file = build_read_attached_file(
        {"1": ("notes.txt", b"allowed contents")}
    )

    assert read_attached_file("1") == (
        "[attached file: notes.txt]\nallowed contents"
    )
    assert read_attached_file("2") == "attachment is not available: 2"


def test_large_document_is_not_returned_as_one_tool_result():
    tools = build_attachment_tools(
        {"1": ("large.txt", ("important content " * 12000).encode())}
    )

    result = tools["read_attached_file"]("1")

    assert "too large for a full read" in result
    assert "summarize_attachment" in result
    assert "search_attachment" in result


def test_retrieval_returns_relevant_page_with_citation():
    contract = ("contract renewal and payment terms " * 1000).strip()
    vacation = ("vacation policy allows thirty paid days " * 1000).strip()
    document = get_document(
        "policy.txt",
        f"{contract}\f{vacation}".encode(),
    )

    chunks = retrieve_chunks(document, "vacation policy paid days", max_chunks=1)

    assert len(chunks) == 1
    assert "vacation policy" in chunks[0].text
    assert "page 2" in chunks[0].sources


async def test_hierarchical_summary_maps_chunks_then_reduces_and_caches():
    chunks = chunk_sections(
        [
            SourceSection(
                "page 1",
                "\n\n".join(f"fact {number} value" for number in range(20)),
            )
        ],
        target_tokens=20,
        overlap_tokens=0,
    )
    document = DocumentIndex(
        filename="report.txt",
        digest="digest",
        sections=[],
        chunks=chunks,
        vectors=[],
    )
    calls = []

    async def complete(system_prompt, content, max_output_tokens):
        calls.append((system_prompt, content, max_output_tokens))
        if system_prompt.startswith("Merge"):
            return "FINAL SUMMARY [page 1]"
        return "chunk note [page 1]"

    first = await summarize_document(document, complete, detail="standard")
    call_count = len(calls)
    second = await summarize_document(document, complete, detail="standard")

    assert first == "FINAL SUMMARY [page 1]\n\nSources covered: page 1"
    assert second == first
    assert call_count == len(chunks) + 1
    assert len(calls) == call_count


def test_attachment_search_tool_formats_sources():
    tools = build_attachment_tools(
        {"1": ("notes.txt", b"alpha topic\n\nbeta answer is 42")}
    )

    result = tools["search_attachment"]("1", "beta answer", max_chunks=1)

    assert "[source: document; chunk: 1]" in result
    assert "beta answer is 42" in result


def test_unsupported_file_type_is_reported():
    assert extract_text("image.png", b"data") == (
        "[unsupported file type: image.png]"
    )


def test_new_attachment_tool_schema_names():
    assert SEARCH_SCHEMA["function"]["name"] == "search_attachment"
    assert SUMMARY_SCHEMA["function"]["name"] == "summarize_attachment"
