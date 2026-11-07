"""解析 Chat Completions 观测副本，不改变实际转发的响应字节。"""

from __future__ import annotations

import json
from typing import Any


def capture_response(body: bytes, *, streaming: bool) -> tuple[Any, list[str]]:
    if not streaming:
        try:
            return json.loads(body), []
        except ValueError, UnicodeError, RecursionError:
            return {"capture_omitted": "response is not valid JSON"}, ["invalid_json"]
    choices: dict[int, dict[str, Any]] = {}
    calls: dict[tuple[int, int], dict[str, Any]] = {}
    issues: list[str] = []
    response: dict[str, Any] = {"choices": [], "stream_done": False}
    # SSE 允许多行 data；拼接字节后解码，避免 UTF-8 和 JSON 跨 chunk 断裂。
    frames = (
        body.removeprefix(b"\xef\xbb\xbf")
        .replace(b"\r\n", b"\n")
        .replace(b"\r", b"\n")
        .split(b"\n\n")
    )
    if frames[-1].strip():
        issues.append("unterminated_sse_frame")
    frames = frames[:-1]
    for frame in frames:
        data = b"\n".join(
            line[5:].removeprefix(b" ") for line in frame.split(b"\n") if line.startswith(b"data:")
        )
        if not data:
            continue
        if data.strip() == b"[DONE]":
            response["stream_done"] = True
            break
        try:
            chunk = json.loads(data)
            if not isinstance(chunk, dict):
                raise TypeError("chunk is not an object")
            for key in ("id", "model", "usage", "error"):
                if key in chunk:
                    response[key] = chunk[key]
            for choice in chunk.get("choices", []):
                index = choice["index"]
                if not isinstance(index, int):
                    raise TypeError("choice index must be an integer")
                target = choices.setdefault(index, {"index": index, "message": {}})
                if choice.get("finish_reason") is not None:
                    target["finish_reason"] = choice["finish_reason"]
                delta = choice.get("delta", {})
                if delta.get("function_call") is not None:
                    issues.append("legacy_function_call_not_reconstructed")
                if any(
                    key not in {"role", "content", "refusal", "tool_calls", "function_call"}
                    for key in delta
                ):
                    issues.append("unsupported_delta_fields")
                message = target["message"]
                for key in ("role", "content", "refusal"):
                    if isinstance(delta.get(key), str):
                        message[key] = (
                            delta[key] if key == "role" else message.get(key, "") + delta[key]
                        )
                for call in delta.get("tool_calls") or []:
                    if call.get("type", "function") != "function":
                        issues.append("non_function_tool_not_reconstructed")
                    call_index = call["index"]
                    if not isinstance(call_index, int):
                        raise TypeError("tool call index must be an integer")
                    tool = calls.setdefault((index, call_index), {"function": {}})
                    for key in ("id", "type"):
                        if isinstance(call.get(key), str):
                            tool[key] = call[key]
                    function = call.get("function") or {}
                    for key in ("name", "arguments"):
                        if isinstance(function.get(key), str):
                            tool["function"][key] = tool["function"].get(key, "") + function[key]
        except ValueError, TypeError, KeyError, AttributeError, RecursionError:
            issues.append("invalid_sse_chunk")
    for (index, _), call in sorted(calls.items()):
        choices[index]["message"].setdefault("tool_calls", []).append(call)
    response["choices"] = [choice for _, choice in sorted(choices.items())]
    if not response["stream_done"]:
        issues.append("missing_stream_done")
    return response, sorted(set(issues))


def tool_requests(response: Any) -> list[dict[str, Any]]:
    """只称为模型请求的调用，不声明工具已执行。"""
    result = []
    if isinstance(response, dict) and isinstance(response.get("choices"), list):
        for choice in response["choices"]:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("tool_calls"), list):
                continue
            result.extend(call for call in message["tool_calls"] if isinstance(call, dict))
    return result


def capture_arguments(value: Any, depth: int = 0) -> Any:
    """在观测副本中解码 arguments，便于既有脱敏器处理自定义敏感字段。"""
    if depth > 10:
        return {"capture_omitted": "maximum nesting depth"}
    if isinstance(value, list):
        return [capture_arguments(item, depth + 1) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "arguments" and isinstance(item, str):
                try:
                    item = json.loads(item)
                except ValueError, RecursionError:
                    pass
            result[key] = capture_arguments(item, depth + 1)
        return result
    return value
