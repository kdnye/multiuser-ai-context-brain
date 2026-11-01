"""A wikilink inside a markdown table escapes its pipe ([[path\\|Title]], Obsidian); the backslash is not the path."""

from tools.wiki.lint import LINK


def test_table_escaped_pipe_is_not_part_of_the_path():
    text = r"| a | [[tasks/x-plan\|Plan]] | [[notes/y|Y]] [[z#h|Z]] [[w]] [[q\#h|Q]] |"
    assert [m.group(1) for m in LINK.finditer(text)] == ["tasks/x-plan", "notes/y", "z", "w", "q"]
