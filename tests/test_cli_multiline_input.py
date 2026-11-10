from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from codeagent.cli import _build_input_session


def test_bracketed_paste_is_one_editable_message_until_enter():
    with create_pipe_input() as pipe:
        session = _build_input_session(input=pipe, output=DummyOutput())
        pipe.send_text(
            "\x1b[200~任务第一段\n\n```text\npytest --exact\n```\n粘贴末尾"
            "\x1b[201~，继续输入后再发送\n"
        )

        message = session.prompt()

    assert message == "任务第一段\n\n```text\npytest --exact\n```\n粘贴末尾，继续输入后再发送"


def test_normal_single_line_input_is_unchanged():
    with create_pipe_input() as pipe:
        session = _build_input_session(input=pipe, output=DummyOutput())
        pipe.send_text("/status\n")
        assert session.prompt() == "/status"
