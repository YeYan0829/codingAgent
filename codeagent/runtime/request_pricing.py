"""冻结价格与单次请求包络；不查询网络、不把估算叫作 Provider 发票。"""
import json


def validate_prices(profiles):
    if not isinstance(profiles, dict):
        raise ValueError("model_token_prices must be a mapping")
    for model, rates in profiles.items():
        if (not isinstance(model, str) or not isinstance(rates, dict)
                or set(rates) != {"input", "cached_input", "output"}
                or any(type(v) is not int or v < 0 for v in rates.values())
                or rates["cached_input"] > rates["input"]):
            raise ValueError("model_token_prices requires input/cached_input/output integer microunits per token")


def input_envelope(request):
    # 对齐 OpenAI-compatible adapter 的消息和 function schema；不采用 bytes/3 的容量估算来承诺费用。
    # UTF-8 字节数 + 显式 framing 余量是保守包络，并非官方 tokenizer。
    from codeagent.model_gateway.openai_client import _to_chat_completion_tool
    wire = {"messages": request.messages, "tools": [_to_chat_completion_tool(t) for t in request.tools]}
    return (len(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            + 1024 + 32 * len(request.messages) + 128 * len(request.tools))


def charge(rates, input_tokens, output_tokens, cached_tokens=0):
    return ((input_tokens - cached_tokens) * rates["input"] + cached_tokens * rates["cached_input"]
            + output_tokens * rates["output"])
