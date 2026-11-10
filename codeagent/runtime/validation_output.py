"""识别运行器明确报告的空测试或失败汇总；未知输出不推断测试数量或覆盖。"""
from __future__ import annotations

import re


_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
_EMPTY_SUMMARIES = (
    # SymPy 自带 bin/test；保留完整总结形状，避免把正文中的“0 passed”当结论。
    re.compile(r'tests finished:\s*0 passed,?\s+in\s+[\d.]+\s+seconds'),
    re.compile(r'no tests ran(?:\s+in\s+[\d.]+s)?'),
    re.compile(r'Ran 0 tests? in [\d.]+s'),
)


def empty_test_summary(stdout: str, stderr: str) -> str | None:
    for output in (stdout, stderr):
        for raw in _ANSI.sub('', output).splitlines():
            line = raw.strip().strip('=').strip()
            if any(pattern.fullmatch(line) for pattern in _EMPTY_SUMMARIES):
                return line
    return None


# 运行器在汇总中明确报告失败但进程仍可能 exit 0（如 SymPy bin/test 的部分模式、管道末端掩盖）。
_FAILED_SUMMARIES = (
    re.compile(r'tests finished:.*\b([1-9]\d*) (?:failed|exceptions?)\b.*'),
    re.compile(r'DO \*NOT\* COMMIT!?'),
    re.compile(r'(?:\d+ passed, )?([1-9]\d*) failed(?:, .*)? in [\d.]+s(?: \(.*\))?'),
    re.compile(r'FAILED \((?:failures|errors)=[1-9]\d*.*\)'),
)


def failed_test_summary(stdout: str, stderr: str) -> str | None:
    for output in (stdout, stderr):
        for raw in _ANSI.sub('', output).splitlines():
            line = raw.strip().strip('=').strip()
            if any(pattern.fullmatch(line) for pattern in _FAILED_SUMMARIES):
                return line
    return None


def reported_validation_issue(payload: dict) -> str | None:
    # 读取旧事件时也识别已保存的总结；不改写历史命令的退出码或评分。
    stdout, stderr = payload.get('stdout_tail', ''), payload.get('stderr_tail', '')
    return payload.get('validation_issue') or (
        'no_tests' if empty_test_summary(stdout, stderr) else
        'reported_failures' if failed_test_summary(stdout, stderr) else None
    )
