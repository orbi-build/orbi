"""Hidden oracle for pyinfra issue 1438: only the public print_rows behaviour."""
from pyinfra_cli.prints import print_rows


def _rows(*rows):
    out = []
    print_rows([(out.append, list(r)) for r in rows])
    return out


def _col(line, marker):
    # display column where marker starts, counting CJK/fullwidth as 2 and
    # zero-width modifiers/combining marks as 0 (terminal truth, hand-rolled)
    import re
    import unicodedata
    prefix = re.sub(r"\x1b\[[0-9;]*m", "", line[: line.index(marker)])
    w = 0
    for ch in prefix:
        cp = ord(ch)
        if unicodedata.combining(ch) or 0x1F3FB <= cp <= 0x1F3FF or cp in (0x200D, 0xFE0F) or cp in (0x3099, 0x309A):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def test_ascii_table_unchanged():
    assert _rows(["Operation", "Hosts"], ["short", "1"]) == [
        "Operation   Hosts   ",
        "short       1       ",
    ]


def test_issue_example_aligned():
    out = _rows(["Operation", "Hosts"], ["server.shell (echo 随便输出一些中文试试)", "1"])
    assert _col(out[0], "Hosts") == _col(out[1], "1")


def test_coloured_cjk_in_later_column_aligned():
    green = "\033[32m成功\033[0m"
    out = _rows(["Name", "Result", "Hosts"], ["a", green, "1"], ["b", "ok", "2"])
    assert _col(out[0], "Hosts") == _col(out[1], "1") == _col(out[2], "2")


def test_emoji_modifier_not_widened():
    out = _rows(["Operation", "Hosts"], ["echo 👍🏽", "1"], ["short", "2"])
    assert _col(out[0], "Hosts") == _col(out[1], "1") == _col(out[2], "2")


def test_nfd_combining_mark_not_widened():
    out = _rows(["Operation", "Hosts"], ["が", "1"], ["short", "2"])
    assert _col(out[0], "Hosts") == _col(out[1], "1") == _col(out[2], "2")
