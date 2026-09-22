ROOT = "00000000-0000-4000-8000-000000000000"
CONVERSATION = "33333333-3333-4333-8333-333333333333"
ORG = "44444444-4444-4444-8444-444444444444"


def block(kind, start, **fields):
    return {"type": kind, "start_timestamp": start, "stop_timestamp": start, **fields}


def message(uuid, parent, sender, content, **extra):
    return {
        "uuid": uuid, "parent_message_uuid": parent, "sender": sender, "text": "",
        "created_at": content[-1]["stop_timestamp"] if content else "2026-09-22T20:00:00Z",
        "content": content, "attachments": [], "files": [], "sync_sources": [], **extra,
    }


def conversation(*messages, leaf=None, **extra):
    return {
        "uuid": CONVERSATION, "name": "Runtime check", "model": "claude-example",
        "created_at": "2026-09-22T19:59:00Z", "updated_at": "2026-09-22T20:01:00Z",
        "current_leaf_message_uuid": leaf or messages[-1]["uuid"],
        "chat_messages": list(messages), **extra,
    }


def tool_turn():
    question = message("q", ROOT, "human", [block("text", "2026-09-22T19:59:21Z", text="What can you see?")])
    answer = message("a", "q", "assistant", [
        block("thinking", "2026-09-22T19:59:22Z", thinking="", thinking_hidden=True,
              summaries=[{"summary": "Checking the runtime."}]),
        block("tool_use", "2026-09-22T19:59:23Z", id="toolu_1", name="Runtime:list",
              input={"ref": "ctx://"}, integration_name="Runtime", mcp_server_url="https://runtime.example/mcp"),
        block("tool_result", "2026-09-22T19:59:24Z", tool_use_id="toolu_1", name="Runtime:list", is_error=False,
              content=[{"type": "text", "text": "registry listing"}], integration_name="Runtime",
              structured_content={"rows": 3}, meta={"io.modelcontextprotocol/serverInfo": {"name": "runtime"}}),
        block("tool_use", "2026-09-22T19:59:25Z", id="toolu_2", name="Runtime:open",
              input={"ref": "ctx://x", "detail": "titles"}, integration_name="Runtime"),
        block("tool_result", "2026-09-22T19:59:26Z", tool_use_id="toolu_2", name="Runtime:open", is_error=True,
              content=[{"type": "text", "text": "open takes detail summary or full"}]),
        block("text", "2026-09-22T19:59:30Z", text="The registry lists three sources."),
    ], stop_reason="end_turn")
    return question, answer
