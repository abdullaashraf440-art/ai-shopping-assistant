"""A tiny scripted chat model so the agent loop can be tested without any API key."""
from typing import Any, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedModel(BaseChatModel):
    script: List[Any]
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                  run_manager=None, **kwargs) -> ChatResult:
        msg = self.script[min(self.cursor, len(self.script) - 1)]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


def call(name, **args):
    return {"name": name, "args": args, "id": f"call_{name}_{abs(hash(str(args))) % 10000}",
            "type": "tool_call"}


def ai(text="", calls=None):
    return AIMessage(content=text, tool_calls=calls or [])
