"""Factory helpers for ADK agents."""

from __future__ import annotations

import os

from google.adk.agents import BaseAgent

from agents.executor_agent import ExecutorAgent
from agents.parser_agent import ParserAgent
from llm.base import LLMClient
from runtime.env import env_bool

_PARSER_AGENT: ParserAgent | None = None
_EXECUTOR_AGENT: ExecutorAgent | None = None


def get_parser_agent(llm_client: LLMClient | None, max_attempts: int = 5) -> ParserAgent:
    """Return the persistent ParserAgent instance (create once, then reuse)."""
    global _PARSER_AGENT
    kb_file = os.getenv("AWARE_PARSER_KB_FILE", "knowledge/parser_buildspec_kb.md")
    enable_reasoning = env_bool("AWARE_ENABLE_REASONING", True)
    enable_memory = env_bool("AWARE_ENABLE_MEMORY", True)
    if _PARSER_AGENT is None:
        _PARSER_AGENT = ParserAgent(
            llm_client=llm_client,
            max_attempts=max_attempts,
            knowledge_file=kb_file,
            enable_reasoning=enable_reasoning,
            enable_memory=enable_memory,
        )
    else:
        _PARSER_AGENT.llm_client = llm_client
        _PARSER_AGENT.max_attempts = max(1, int(max_attempts))
        _PARSER_AGENT.knowledge_file = kb_file
        _PARSER_AGENT.enable_reasoning = enable_reasoning
        _PARSER_AGENT.enable_memory = enable_memory
    agent = _PARSER_AGENT
    if not isinstance(agent, BaseAgent):
        raise TypeError("ParserAgent must inherit from google.adk.agents.BaseAgent.")
    return agent


def create_parser_agent(llm_client: LLMClient | None, max_attempts: int = 5) -> ParserAgent:
    """Backward-compatible alias."""
    return get_parser_agent(llm_client=llm_client, max_attempts=max_attempts)


def get_executor_agent(llm_client: LLMClient | None = None) -> ExecutorAgent:
    """Return persistent ExecutorAgent instance."""
    global _EXECUTOR_AGENT
    enable_reasoning = env_bool("AWARE_ENABLE_REASONING", True)
    enable_memory = env_bool("AWARE_ENABLE_MEMORY", True)
    os.environ["AWARE_RCA_VERSION"] = "v3"
    execution_mode = "v3"
    kb_file = os.getenv("AWARE_EXECUTOR_V3_KB_FILE", "knowledge/executor_rca_v3_kb.md")
    if _EXECUTOR_AGENT is None:
        _EXECUTOR_AGENT = ExecutorAgent(
            llm_client=llm_client,
            knowledge_file=kb_file,
            enable_reasoning=enable_reasoning,
            enable_memory=enable_memory,
            execution_mode=execution_mode,
        )
    else:
        _EXECUTOR_AGENT.llm_client = llm_client
        _EXECUTOR_AGENT.knowledge_file = kb_file
        _EXECUTOR_AGENT.enable_reasoning = enable_reasoning
        _EXECUTOR_AGENT.enable_memory = enable_memory
        _EXECUTOR_AGENT.execution_mode = "v3"
    agent = _EXECUTOR_AGENT
    if not isinstance(agent, BaseAgent):
        raise TypeError("ExecutorAgent must inherit from google.adk.agents.BaseAgent.")
    return agent


def create_executor_agent(llm_client: LLMClient | None = None) -> ExecutorAgent:
    """Backward-compatible alias."""
    return get_executor_agent(llm_client=llm_client)
