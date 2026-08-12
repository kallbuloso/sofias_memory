from __future__ import annotations

from uuid import uuid4

from sofias_memory.pipelines.chunking import TextTokenizer, chunk_document_text


def test_chunk_document_text_is_stable_ordered_and_offset_preserving() -> None:
    document_id = uuid4()
    tokenizer = TextTokenizer("text-embedding-3-large")
    text = (
        "Alpha keeps a short opening paragraph.\n\n"
        "Beta has enough repeated context to force a deterministic split. "
        * 12
        + "\nGamma closes the document."
    )

    first = chunk_document_text(
        text,
        document_id=document_id,
        generation=0,
        tokenizer=tokenizer,
        max_tokens=24,
        overlap_tokens=6,
        min_tokens=4,
    )
    second = chunk_document_text(
        text,
        document_id=document_id,
        generation=0,
        tokenizer=tokenizer,
        max_tokens=24,
        overlap_tokens=6,
        min_tokens=4,
    )

    assert first == second
    assert [chunk.ordinal for chunk in first] == list(range(len(first)))
    assert len(first) > 1
    for chunk in first:
        assert chunk.text == text[chunk.start_char : chunk.end_char]
        assert chunk.token_count <= 24
        assert len(chunk.content_sha256) == 64
    assert any(
        next_chunk.start_char < chunk.end_char
        for chunk, next_chunk in zip(first, first[1:], strict=True)
    )
    assert [chunk.id for chunk in first] == [chunk.id for chunk in second]


def test_small_document_still_produces_one_chunk() -> None:
    document_id = uuid4()
    tokenizer = TextTokenizer("unknown-openai-compatible-model")
    text = "Tiny but meaningful."

    chunks = chunk_document_text(
        text,
        document_id=document_id,
        generation=1,
        tokenizer=tokenizer,
        max_tokens=100,
        overlap_tokens=10,
        min_tokens=40,
    )

    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].start_char == 0
    assert chunks[0].end_char == len(text)
