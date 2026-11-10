"""主、子循环共用的截断恢复策略；恢复仍使用原执行账本与期限。"""

MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS = 3
MAX_PROTOCOL_REPAIRS = 3


def protocol_repair_message(error):
    return (f"上一响应中 {error.get('tool', '')} 的 arguments 不是合法 JSON 对象：{error.get('message', '')} "
            f"(line {error.get('line')}, column {error.get('column')})。"
            "本次响应中的工具均未执行；之前已完成的操作保留。"
            "请依据工具 schema 重新生成完整工具调用，参数必须是对象，并正确转义字符串。")


def protocol_repair_exhausted(error, previous):
    return len(previous) >= MAX_PROTOCOL_REPAIRS or bool(
        previous and previous[0].get("fingerprint") == error.get("fingerprint"))


def recovery_reasoning_effort(config, *, recovering: bool) -> str | None:
    """仅对支持的模型临时降强度；None 表示继续采用原配置。"""
    if not recovering or not config.reasoning_enabled:
        return None
    if config.provider == "deepseek" or (
        config.provider == "glm" and config.resolved_model == "glm-5.3"
    ):
        return "low"
    return None


def output_truncation_reason(response, max_tokens):
    """统一识别显式长度结束和仅有 usage 的空响应，主子使用同一规则。"""
    if response.finish_reason == 'length':
        return 'provider_finish_reason'
    if response.finish_reason is not None:
        return None
    output_tokens = response.usage.output_tokens if response.usage is not None else None
    if not response.text and not response.tool_calls and max_tokens is not None and output_tokens is not None and output_tokens >= max_tokens:
        return 'usage_fallback'
    return None
