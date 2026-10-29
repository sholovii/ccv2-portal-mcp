from ccv2_portal_mcp import buildlog
from tests.conftest import make_zip

LOG = "\n".join(["line one", "compiling", "[ERROR] Foo.java:12: cannot find symbol", "  symbol: Bar", "BUILD FAILED", "done"])


def test_extract_largest_first():
    entries = buildlog.extract_entries(make_zip({"small.txt": "x", "big.log": LOG}))
    assert entries[0][0] == "big.log"


def test_first_error_window():
    lines = buildlog.first_error(LOG, before=1, after=2)
    assert lines[0].startswith("2: compiling")
    assert any("cannot find symbol" in l for l in lines)


def test_grep_with_context_and_cap():
    lines, total, truncated = buildlog.select_lines(LOG, grep="failed", context=1, max_lines=10)
    assert total == 6
    assert [l.split(":")[0] for l in lines] == ["4", "5", "6"]
    assert not truncated


def test_tail_truncation_marks():
    text = "\n".join(str(i) for i in range(100))
    lines, total, truncated = buildlog.select_lines(text, tail=80, max_lines=10)
    assert truncated and len(lines) == 10 and lines[-1].endswith("99")


def test_first_error_ignores_exception_class_names():
    # a successful build log lists files such as FooException.class; that is not an error
    text = "INFO stdout: [delete] Deleting x/DbAppenderLoggingException.class\nINFO done\n"
    assert buildlog.first_error(text) is None
    text += "Caused by: java.lang.IllegalStateException: boom\n"
    assert buildlog.first_error(text)[-1].endswith("IllegalStateException: boom")
