from collections.abc import Callable

from .indexing import get_document
from .models import Completion, DocumentIndex
from .retrieval import DEFAULT_RETRIEVAL_CHUNKS, retrieve_chunks
from .summarization import summarize_document


DIRECT_READ_MAX_TOKENS = 5000


def build_attachment_tools(
    attachments: dict[str, tuple[str, bytes]],
    complete: Completion | None = None,
) -> dict[str, Callable]:
    def resolve(attachment_id: str) -> DocumentIndex | None:
        attachment = attachments.get(str(attachment_id))
        return get_document(*attachment) if attachment is not None else None

    def read_attached_file(attachment_id: str) -> str:
        document = resolve(attachment_id)
        if document is None:
            return f"attachment is not available: {attachment_id}"
        if not document.chunks:
            return f"[no extractable text in {document.filename}]"
        if document.token_count > DIRECT_READ_MAX_TOKENS:
            return (
                "document is too large for a full read: "
                f"chunks={len(document.chunks)}, "
                f"estimated_tokens={document.token_count}. "
                "Use summarize_attachment for a whole-document summary or "
                "search_attachment for a specific question."
            )
        text = "\n\n".join(
            section.text for section in document.sections if section.text
        )
        return f"[attached file: {document.filename}]\n{text}"

    def search_attachment(
        attachment_id: str,
        query: str,
        max_chunks: int = DEFAULT_RETRIEVAL_CHUNKS,
    ) -> str:
        document = resolve(attachment_id)
        if document is None:
            return f"attachment is not available: {attachment_id}"
        chunks = retrieve_chunks(document, query, max_chunks)
        if not chunks:
            return "no relevant text found in the attachment"
        return "\n\n".join(
            f"[source: {chunk.citation}; chunk: {chunk.index + 1}]\n{chunk.text}"
            for chunk in chunks
        )

    async def summarize_attachment(
        attachment_id: str,
        detail: str = "standard",
    ) -> str:
        document = resolve(attachment_id)
        if document is None:
            return f"attachment is not available: {attachment_id}"
        if complete is None:
            return "document summarization is unavailable in this request"
        return await summarize_document(document, complete, detail)

    return {
        "read_attached_file": read_attached_file,
        "search_attachment": search_attachment,
        "summarize_attachment": summarize_attachment,
    }


def build_read_attached_file(
    attachments: dict[str, tuple[str, bytes]],
) -> Callable[[str], str]:
    """Compatibility wrapper around the request-scoped attachment registry."""
    return build_attachment_tools(attachments)["read_attached_file"]
