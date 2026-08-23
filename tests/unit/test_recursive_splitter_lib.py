from libs.splitter import RecursiveSplitter


def test_recursive_splitter_is_deterministic_with_overlap() -> None:
    text = " ".join(f"MOCA-token-{index}" for index in range(80))
    splitter = RecursiveSplitter(chunk_size=140, chunk_overlap=25)

    first = splitter.split_text(text)
    second = splitter.split_text(text)

    assert first == second
    assert len(first) > 1
    assert all(len(chunk) <= 140 for chunk in first)
    assert all(chunk in text for chunk in first)


def test_fenced_code_block_is_not_split() -> None:
    code = "```moca\n" + "publish data where command = 'configure';\n" * 8 + "```"
    text = f"# Policy setup\n\nBefore the command.\n\n{code}\n\nAfter the command."
    splitter = RecursiveSplitter(chunk_size=100, chunk_overlap=10)

    chunks = splitter.split_text(text)

    assert code in chunks
    assert sum("```moca" in chunk for chunk in chunks) == 1
