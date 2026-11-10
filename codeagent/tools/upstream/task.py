"""OpenHands TaskAction有效字段及工具提示；MIT来源见随包LICENSE.openhands-sdk。

固定来源：704cbe6015e3d59cabe04632175d99df2d448999，tools/task/definition.py。
仅采用四个有效输入字段；不引入已废弃且不生效的max_turns或上游执行器。
"""
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    description: str | None = Field(default=None, description="A short (3-5 word) description of the task.")
    prompt: str = Field(description="The task for the agent to perform.")
    subagent_type: str = Field(default="general-purpose", description="The type of specialized agent to use for this task.")
    resume: str | None = Field(default=None, description="Task ID of the task to resume from.")


TASK_TOOL_DESCRIPTION: Final[
    str
] = """Launch a subagent to handle complex, multi-step tasks autonomously.

Subagents are autonomous agents that work independently and return results to you. They are your primary tool for understanding codebases and running
tests, but each delegation has overhead — use them when the task genuinely benefits from a separate agent, not for simple lookups.

Available agent types and the tools they have access to:
{agent_types_info}

When NOT to use the task tool:
- A single grep, find, or cat command would answer your question — just run it yourself
- You are making a file edit (use file_editor directly)
- You already have the context needed

When using the task tool:
- Write a detailed prompt describing exactly what you need
- Include specific file paths, class names, or error messages from the issue
- Tell the agent what to report back (file paths, line numbers, code snippets)
- The agent's results are authoritative — verify subagent results only when the task involves judgment or
  interpretation.
  
{task_tool_examples}
"""

TASK_TOOL_EXAMPLES: Final[dict[str, str]] = {
    "code-explorer": """
Example — Multi-step exploration (good use of code-explorer):
    subagent_type="code-explorer"
    prompt="Trace how the DateFormat.y() method is called through Django's
    template system. Find: (1) the method definition, (2) where it's
    registered as a format character, (3) all test cases. Include code
    snippets and file paths."
""",
    "bash-runner": """
Example — Running tests (good use of bash-runner):
    subagent_type="bash-runner"
    prompt="Run: cd /workspace/django && python tests/runtests.py
    utils_tests.test_dateformat -v 2. Provide a summary including
    the total tests run, the final status, and a list of any
    failing test names. For each failure, include the specific
    cause or assertion error, but do not include the full stack
    trace or the verbose setup/teardown output."
""",
    "web researcher": """
Example — Research information on a website (good use of web researcher):
    subagent_type="web researcher"
    prompt="Navigate to the Stripe API docs and find the parameters for the PaymentIntent create endpoint."
""",  # noqa: E501
    "general purpose": """
Example — Perform a multi-step task involving code editing and shell commands:
    subagent_type="general purpose"
    prompt="Read the database module in src/db.py, extract the connection
    pooling logic into a separate file, update all imports, and run the
    test suite to verify nothing breaks."
""",
}
