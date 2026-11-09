import pytest
from check_comments import check_sources


def test_stage3_code_does_not_need_blanket_comments(tmp_path):
    source = tmp_path / "src" / "execution.py"
    source.parent.mkdir()
    source.write_text("class Execution:\n    def run(self):\n        return 'passed'\n")
    check_sources(tmp_path)

    source.write_text("def broken(:\n")
    with pytest.raises(SyntaxError):
        check_sources(tmp_path)
