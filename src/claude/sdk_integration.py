"""Claude Code Python SDK integration."""

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    Message,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import RateLimitEvent, StreamEvent

from ..config.settings import Settings
from ..security.validators import SecurityValidator
from .exceptions import (
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)
from .monitor import _is_claude_internal_path, check_bash_directory_boundary

logger = structlog.get_logger()

# Fallback message when Claude produces no text but did use tools.
TURN_LIMIT_MSG = (
    "⚠️ Stopped: hit the turn limit ({turns} turns) before finishing. "
    "Say 'continue' to resume where it left off."
)
TASK_COMPLETED_MSG = "✅ Task completed. Tools used: {tools_summary}"


@dataclass
class ClaudeResponse:
    """Response from Claude Code SDK."""

    content: str
    session_id: str
    cost: float
    duration_ms: int
    num_turns: int
    is_error: bool = False
    error_type: Optional[str] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)
    interrupted: bool = False
    # True when Claude wrote no reply text at all. `content` may still be a
    # stand-in ("Task completed. Tools used: …"), so this is the real signal.
    no_text: bool = False
    # The run was stopped by a usage limit. `limit_resets_at` is when that
    # window reopens (unix seconds), when the CLI said so.
    limit_hit: bool = False
    limit_resets_at: Optional[float] = None
    # Size of the conversation as the model last saw it. Not a running total:
    # every turn resends the whole history, so the newest turn's input count
    # *is* the current context size.
    context_tokens: Optional[int] = None
    # The model that actually produced this reply, as reported by the SDK.
    # The banner falls back to this when the session has made no `/model`
    # choice and no default is pinned — otherwise the model name would be
    # dropped from the footer even though a real model clearly answered.
    model_id: Optional[str] = None


_LIMIT_TEXT = ("usage limit", "hit your limit", "limit reached")


def _limit_hit(
    messages: List[Any], result_is_error: bool = False, content: str = ""
) -> "tuple[bool, Optional[float]]":
    """Whether a usage limit stopped this run, and when its window reopens.

    The CLI announces it as a rate-limit event whose status is `rejected`. If
    it did not (older CLI, odd stream), an error result that reads like a limit
    message still counts, just without a reset time.
    """
    hit = False
    resets: Optional[float] = None
    for message in messages:
        if not isinstance(message, RateLimitEvent):
            continue
        info = message.rate_limit_info
        # `overage` is the pay-as-you-go pool; its refusal is not "you are out".
        if info.status != "rejected" or info.rate_limit_type == "overage":
            continue
        hit = True
        if info.resets_at:
            resets = float(info.resets_at)
    if not hit and result_is_error:
        lowered = (content or "").lower()
        hit = any(phrase in lowered for phrase in _LIMIT_TEXT)
    return hit, resets


def _last_model(messages: List[Any]) -> Optional[str]:
    """The model string of the last assistant turn, or None if there was none.

    Walks from the end because the very last assistant message is the one that
    produced the user-visible reply; an earlier one in a multi-turn run may
    have been a different model (e.g. a sub-agent) and would be misleading in
    the banner.
    """
    for message in reversed(messages):
        if not isinstance(message, AssistantMessage):
            continue
        # A sub-agent's turns carry the id of the tool call that spawned them;
        # they may run a different model and are not the one answering.
        if getattr(message, "parent_tool_use_id", None):
            continue
        model = getattr(message, "model", None)
        if isinstance(model, str) and model:
            return model
    return None


def _context_tokens(messages: List[Any]) -> Optional[int]:
    """How much context the model was holding on its last turn.

    Everything the model reads is resent on every turn, so one turn's input
    count — prompt plus whatever was served from cache — is the size of the
    conversation at that moment. Output tokens are left out: they are part of
    the *next* turn's input, not this one's.

    Only assistant messages are consulted. ``ResultMessage.usage`` looks like
    the same thing but is a *total for the turn*: a turn with three tool calls
    reports three times the cache reads, which is three times the context and
    plainly wrong. The last assistant message is the one to ask — an earlier
    one in the same turn had seen less of the conversation.
    """
    for message in reversed(messages):
        if not isinstance(message, AssistantMessage):
            continue
        usage = getattr(message, "usage", None)
        if not isinstance(usage, dict):
            continue
        total = 0
        for key in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                total += int(value)
        if total:
            return total
    return None


@dataclass
class StreamUpdate:
    """Streaming update from Claude SDK."""

    type: str  # 'assistant', 'user', 'system', 'result', 'stream_delta'
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None
    progress: Optional[Dict[str, Any]] = None

    def is_error(self) -> bool:
        """Check whether this stream update represents an error."""
        if self.type == "error":
            return True

        if self.metadata:
            if self.metadata.get("is_error") is True:
                return True
            status = self.metadata.get("status")
            if isinstance(status, str) and status.lower() == "error":
                return True
            error_val = self.metadata.get("error")
            if isinstance(error_val, str) and error_val:
                return True
            error_msg_val = self.metadata.get("error_message")
            if isinstance(error_msg_val, str) and error_msg_val:
                return True

        if self.progress:
            status = self.progress.get("status")
            if isinstance(status, str) and status.lower() == "error":
                return True

        return False

# Tools that need an interactive UI the bot cannot draw. Asking over Telegram
# means asking in plain text, so these are denied with an instruction the model
# can act on rather than left to time out and return nothing.
_INTERACTIVE_TOOLS = {
    "AskUserQuestion": (
        "AskUserQuestion is not available over Telegram — there is no UI to "
        "render the choices, so the answer would come back empty. Ask the "
        "question as plain text in your reply instead (numbered options are "
        "fine), then stop and wait for the user to answer."
    ),
}


def _make_can_use_tool_callback(
    security_validator: Optional[SecurityValidator],
    working_directory: Path,
    approved_directory: Path,
    ask_user: Optional[Any] = None,
) -> Any:
    """Create a can_use_tool callback for SDK-level tool permission validation.

    The callback validates file path boundaries and bash directory boundaries
    *before* the SDK executes the tool, providing preventive security
    enforcement, and blocks tools that require an interactive UI.
    """
    _FILE_TOOLS = {"Write", "Edit", "Read", "create_file", "edit_file", "read_file"}
    _BASH_TOOLS = {"Bash", "bash", "shell"}

    async def can_use_tool(
        tool_name: str,
        tool_input: Dict[str, Any],
        context: ToolPermissionContext,
    ) -> Any:
        # Interactive tools have no Telegram equivalent. Denying with a reason
        # turns a dead call into a redirect: the model reads the message as the
        # tool result and re-asks in prose.
        if tool_name == "AskUserQuestion" and ask_user is not None:
            # The callback is awaited inside the turn, so the model can wait
            # here for a button without its turn ending. The answer has to
            # ride in a deny message: this hook may allow or refuse, never
            # return a tool result.
            try:
                answer = await ask_user(tool_input)
            except Exception as exc:
                logger.warning("can_use_tool: asking failed", error=str(exc))
                answer = None
            if answer:
                logger.info("can_use_tool answered interactive tool")
                return PermissionResultDeny(
                    message=(
                        "The user answered by tapping a button:\n"
                        f"{answer}\n"
                        "Treat this as the result of the call and carry on."
                    )
                )
            # Nobody answered; fall through to the prose instruction.

        if tool_name in _INTERACTIVE_TOOLS:
            logger.info("can_use_tool denied interactive tool", tool_name=tool_name)
            return PermissionResultDeny(message=_INTERACTIVE_TOOLS[tool_name])

        if security_validator is None:
            return PermissionResultAllow()

        # File path validation
        if tool_name in _FILE_TOOLS:
            file_path = tool_input.get("file_path") or tool_input.get("path")
            if file_path:
                # Allow Claude Code internal paths (~/.claude/plans/, etc.)
                if _is_claude_internal_path(file_path):
                    return PermissionResultAllow()

                valid, _resolved, error = security_validator.validate_path(
                    file_path, working_directory
                )
                if not valid:
                    logger.warning(
                        "can_use_tool denied file operation",
                        tool_name=tool_name,
                        file_path=file_path,
                        error=error,
                    )
                    return PermissionResultDeny(message=error or "Invalid file path")

        # Bash directory boundary validation
        if tool_name in _BASH_TOOLS:
            command = tool_input.get("command", "")
            if command:
                valid, error = check_bash_directory_boundary(
                    command, working_directory, approved_directory
                )
                if not valid:
                    logger.warning(
                        "can_use_tool denied bash command",
                        tool_name=tool_name,
                        command=command,
                        error=error,
                    )
                    return PermissionResultDeny(
                        message=error or "Bash directory boundary violation"
                    )

        return PermissionResultAllow()

    return can_use_tool


class ClaudeSDKManager:
    """Manage Claude Code SDK integration."""

    def __init__(
        self,
        config: Settings,
        security_validator: Optional[SecurityValidator] = None,
    ):
        """Initialize SDK manager with configuration."""
        self.config = config
        self.security_validator = security_validator

        # Set up environment for Claude Code SDK if API key is provided
        # If no API key is provided, the SDK will use existing CLI authentication
        if config.anthropic_api_key_str:
            os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key_str
            logger.info("Using provided API key for Claude SDK authentication")
        else:
            logger.info("No API key provided, using existing Claude CLI authentication")

    def _is_retryable_error(self, exc: BaseException) -> bool:
        """Return True for transient errors that warrant a retry.
        asyncio.TimeoutError is intentional (user-configured timeout) — not retried.
        Only non-MCP CLIConnectionError is considered transient.
        """
        if isinstance(exc, CLIConnectionError):
            msg = str(exc).lower()
            return "mcp" not in msg  # "server" alone is too broad
        return False

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        images: Optional[List[Dict[str, str]]] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        ask_user: Optional[Any] = None,
    ) -> ClaudeResponse:
        """Execute Claude Code command via SDK.

        ``model`` overrides the configured one for this call only — the
        options object is rebuilt per request, so the choice is naturally
        per-invocation rather than process-wide.
        """
        start_time = asyncio.get_event_loop().time()

        logger.info(
            "Starting Claude SDK command",
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
        )

        try:
            # Capture stderr from Claude CLI for better error diagnostics
            stderr_lines: List[str] = []

            def _stderr_callback(line: str) -> None:
                stderr_lines.append(line)
                logger.debug("Claude CLI stderr", line=line)

            # Build system prompt, loading CLAUDE.md from working directory if present
            base_prompt = (
                f"All file operations must stay within {working_directory}. "
                "Use relative paths."
            )
            claude_md_path = Path(working_directory) / "CLAUDE.md"
            if claude_md_path.exists():
                base_prompt += "\n\n" + claude_md_path.read_text(encoding="utf-8")
                logger.info(
                    "Loaded CLAUDE.md into system prompt",
                    path=str(claude_md_path),
                )

            # When DISABLE_TOOL_VALIDATION=true, pass None for allowed/disallowed
            # tools so the SDK does not restrict tool usage (e.g. MCP tools).
            if self.config.disable_tool_validation:
                sdk_allowed_tools = None
                sdk_disallowed_tools = None
            else:
                sdk_allowed_tools = self.config.claude_allowed_tools
                sdk_disallowed_tools = self.config.claude_disallowed_tools

            # Build Claude Agent options
            options = ClaudeAgentOptions(
                max_turns=self.config.claude_max_turns,
                model=model or self.config.claude_model or None,
                effort=effort or self.config.claude_effort or None,
                # Replay the resumed history into a *new* session id instead
                # of appending to the old one, leaving the original intact.
                # One Read of a screenshot exceeds the SDK's 1 MB default and
                # kills the transport for the whole turn.
                max_buffer_size=self.config.claude_max_buffer_mb * 1024 * 1024,
                max_budget_usd=self.config.claude_max_cost_per_request,
                cwd=str(working_directory),
                allowed_tools=sdk_allowed_tools,
                disallowed_tools=sdk_disallowed_tools,
                cli_path=self.config.claude_cli_path or None,
                include_partial_messages=stream_callback is not None,
                sandbox={
                    "enabled": self.config.sandbox_enabled,
                    "autoAllowBashIfSandboxed": True,
                    "excludedCommands": self.config.sandbox_excluded_commands or [],
                },
                system_prompt=base_prompt,
                setting_sources=["project"],
                stderr=_stderr_callback,
            )

            # Pass MCP server configuration if enabled
            if self.config.enable_mcp and self.config.mcp_config_path:
                options.mcp_servers = self._load_mcp_config(self.config.mcp_config_path)
                logger.info(
                    "MCP servers configured",
                    mcp_config_path=str(self.config.mcp_config_path),
                )

            # Wire can_use_tool callback for preventive tool validation. Always
            # wired, even without a security validator, because it is also what
            # blocks interactive-only tools like AskUserQuestion.
            options.can_use_tool = _make_can_use_tool_callback(
                security_validator=self.security_validator,
                working_directory=working_directory,
                approved_directory=self.config.approved_directory,
                ask_user=ask_user,
            )

            # Resume previous session if we have a session_id
            if session_id and continue_session:
                options.resume = session_id
                logger.info(
                    "Resuming previous session",
                    session_id=session_id,
                )

            # Collect messages via ClaudeSDKClient
            messages: List[Message] = []
            interrupted = False

            async def _run_client() -> None:
                client = ClaudeSDKClient(options)
                try:
                    await client.connect()

                    if images:
                        content_blocks: List[Dict[str, Any]] = []
                        for img in images:
                            media_type = img.get("media_type", "image/png")
                            content_blocks.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": img["data"],
                                    },
                                }
                            )
                        content_blocks.append({"type": "text", "text": prompt})

                        multimodal_msg = {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": content_blocks,
                            },
                        }

                        async def _multimodal_prompt() -> AsyncIterator[Dict[str, Any]]:
                            yield multimodal_msg

                        await client.query(_multimodal_prompt())
                    else:
                        await client.query(prompt)

                    async for raw_data in client._query.receive_messages():
                        try:
                            message = parse_message(raw_data)
                        except MessageParseError as e:
                            logger.debug(
                                "Skipping unparseable message",
                                error=str(e),
                            )
                            continue

                        messages.append(message)

                        if isinstance(message, ResultMessage):
                            break

                        # Handle streaming callback
                        if stream_callback:
                            try:
                                await self._handle_stream_message(
                                    message, stream_callback
                                )
                            except Exception as callback_error:
                                logger.warning(
                                    "Stream callback failed",
                                    error=str(callback_error),
                                    error_type=type(callback_error).__name__,
                                )
                finally:
                    await client.disconnect()

            # Execute with timeout and retry, racing against optional interrupt
            max_attempts = max(1, self.config.claude_retry_max_attempts)
            last_exc: Optional[BaseException] = None

            for attempt in range(max_attempts):
                # Reset message accumulator each attempt so that a failed attempt
                # does not pollute the next one with partial/duplicate messages.
                # _run_client() closes over `messages` by reference (late-binding
                # closure), so clearing it here is seen by every new call.
                messages.clear()

                if attempt > 0:
                    delay = min(
                        self.config.claude_retry_base_delay
                        * (self.config.claude_retry_backoff_factor ** (attempt - 1)),
                        self.config.claude_retry_max_delay,
                    )
                    logger.warning(
                        "Retrying Claude SDK command",
                        attempt=attempt + 1,
                        max_attempts=max_attempts,
                        delay_seconds=delay,
                    )
                    await asyncio.sleep(delay)

                run_task = asyncio.create_task(_run_client())

                interrupt_watcher: Optional["asyncio.Task[None]"] = None
                if interrupt_event is not None:

                    async def _cancel_on_interrupt() -> None:
                        nonlocal interrupted
                        await interrupt_event.wait()
                        interrupted = True
                        run_task.cancel()

                    interrupt_watcher = asyncio.create_task(_cancel_on_interrupt())

                # Note: asyncio.TimeoutError is intentionally NOT retried —
                # it reflects a user-configured hard limit.
                try:
                    await asyncio.wait_for(
                        asyncio.shield(run_task),
                        timeout=self.config.claude_timeout_seconds,
                    )
                    break  # success — exit retry loop
                except asyncio.CancelledError:
                    if not interrupted:
                        raise
                    # Interrupt cancelled the task — wait for cleanup
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        pass
                    break  # user interrupted — don't retry
                except asyncio.TimeoutError:
                    run_task.cancel()
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        pass
                    raise  # timeout — don't retry
                except CLIConnectionError as exc:
                    if self._is_retryable_error(exc) and attempt < max_attempts - 1:
                        last_exc = exc
                        logger.warning(
                            "Transient connection error, will retry",
                            attempt=attempt + 1,
                            error=str(exc),
                        )
                        continue
                    raise  # non-retryable or attempts exhausted
                finally:
                    if interrupt_watcher is not None:
                        interrupt_watcher.cancel()
            else:
                if last_exc is not None:
                    raise last_exc

            # Extract cost, tools, and session_id from result message
            cost = 0.0
            tools_used: List[Dict[str, Any]] = []
            claude_session_id = None
            result_content = None
            result_is_error = False
            hit_turn_limit = False
            for message in messages:
                if isinstance(message, ResultMessage):
                    hit_turn_limit = (
                        getattr(message, "subtype", None) == "error_max_turns"
                    )
                    cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                    claude_session_id = getattr(message, "session_id", None)
                    result_content = getattr(message, "result", None)
                    result_is_error = bool(getattr(message, "is_error", False))
                    current_time = asyncio.get_event_loop().time()
                    for msg in messages:
                        if isinstance(msg, AssistantMessage):
                            msg_content = getattr(msg, "content", [])
                            if msg_content and isinstance(msg_content, list):
                                for block in msg_content:
                                    if isinstance(block, ToolUseBlock):
                                        tools_used.append(
                                            {
                                                "name": getattr(
                                                    block, "name", "unknown"
                                                ),
                                                "timestamp": current_time,
                                                "input": getattr(block, "input", {}),
                                            }
                                        )
                    break

            # Fallback: extract session_id from StreamEvent messages if
            # ResultMessage didn't provide one (can happen with some CLI versions)
            if not claude_session_id:
                for message in messages:
                    msg_session_id = getattr(message, "session_id", None)
                    if msg_session_id and not isinstance(message, ResultMessage):
                        claude_session_id = msg_session_id
                        logger.info(
                            "Got session ID from stream event (fallback)",
                            session_id=claude_session_id,
                        )
                        break

            # Calculate duration
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

            # Use Claude's session_id if available, otherwise fall back
            final_session_id = claude_session_id or session_id or ""

            if claude_session_id and claude_session_id != session_id:
                logger.info(
                    "Got session ID from Claude",
                    claude_session_id=claude_session_id,
                    previous_session_id=session_id,
                )

            # A turn-limit stop has no final answer; whatever text came before
            # is mid-task chatter, so say what happened instead of showing it.
            if hit_turn_limit:
                logger.warning(
                    "Claude stopped at turn limit",
                    max_turns=self.config.claude_max_turns,
                )
                content = TURN_LIMIT_MSG.format(turns=self.config.claude_max_turns)
            # Use ResultMessage.result if available, fall back to message extraction
            elif result_content is not None:
                content = str(result_content).strip()
            else:
                content_parts = []
                for msg in messages:
                    if isinstance(msg, AssistantMessage):
                        msg_content = getattr(msg, "content", [])
                        if msg_content and isinstance(msg_content, list):
                            for block in msg_content:
                                if hasattr(block, "text"):
                                    content_parts.append(block.text)
                        elif msg_content:
                            content_parts.append(str(msg_content))
                content = "\n".join(content_parts).strip()

            no_text = not content and not hit_turn_limit
            if not content and tools_used:
                tool_names = [
                    tool.get("name", "")
                    for tool in tools_used
                    if isinstance(tool.get("name"), str) and tool.get("name")
                ]
                unique_tool_names = list(dict.fromkeys(tool_names))
                tools_summary = ", ".join(unique_tool_names) or "unknown"
                content = TASK_COMPLETED_MSG.format(tools_summary=tools_summary)

            limit_hit, limit_resets_at = _limit_hit(
                messages, result_is_error, content
            )

            return ClaudeResponse(
                content=content,
                session_id=final_session_id,
                cost=cost,
                duration_ms=duration_ms,
                num_turns=len(
                    [
                        m
                        for m in messages
                        if isinstance(m, (UserMessage, AssistantMessage))
                    ]
                ),
                tools_used=tools_used,
                interrupted=interrupted,
                no_text=no_text,
                limit_hit=limit_hit,
                limit_resets_at=limit_resets_at,
                context_tokens=_context_tokens(messages),
                model_id=_last_model(messages),
            )

        except asyncio.TimeoutError:
            logger.error(
                "Claude SDK command timed out",
                timeout_seconds=self.config.claude_timeout_seconds,
            )
            raise ClaudeTimeoutError(
                f"Claude SDK timed out after {self.config.claude_timeout_seconds}s"
            )

        except CLINotFoundError as e:
            logger.error("Claude CLI not found", error=str(e))
            error_msg = (
                "Claude Code not found. Please ensure Claude is installed:\n"
                "  npm install -g @anthropic-ai/claude-code\n\n"
                "If already installed, try one of these:\n"
                "  1. Add Claude to your PATH\n"
                "  2. Create a symlink: ln -s $(which claude) /usr/local/bin/claude\n"
                "  3. Set CLAUDE_CLI_PATH environment variable"
            )
            raise ClaudeProcessError(error_msg)

        except ProcessError as e:
            error_str = str(e)
            # Include captured stderr for better diagnostics
            captured_stderr = "\n".join(stderr_lines[-20:]) if stderr_lines else ""
            if captured_stderr:
                error_str = f"{error_str}\nStderr: {captured_stderr}"
            logger.error(
                "Claude process failed",
                error=error_str,
                exit_code=getattr(e, "exit_code", None),
                stderr=captured_stderr or None,
            )
            # Check if the process error is MCP-related
            if "mcp" in error_str.lower():
                raise ClaudeMCPError(f"MCP server error: {error_str}")
            raise ClaudeProcessError(f"Claude process error: {error_str}")

        except CLIConnectionError as e:
            error_str = str(e)
            logger.error("Claude connection error", error=error_str)
            # Check if the connection error is MCP-related
            if "mcp" in error_str.lower() or "server" in error_str.lower():
                raise ClaudeMCPError(f"MCP server connection failed: {error_str}")
            raise ClaudeProcessError(f"Failed to connect to Claude: {error_str}")

        except CLIJSONDecodeError as e:
            logger.error("Claude SDK JSON decode error", error=str(e))
            raise ClaudeParsingError(f"Failed to decode Claude response: {str(e)}")

        except ClaudeSDKError as e:
            logger.error("Claude SDK error", error=str(e))
            raise ClaudeProcessError(f"Claude SDK error: {str(e)}")

        except Exception as e:
            exceptions = getattr(e, "exceptions", None)
            if exceptions is not None:
                # ExceptionGroup from TaskGroup operations (Python 3.11+)
                logger.error(
                    "Task group error in Claude SDK",
                    error=str(e),
                    error_type=type(e).__name__,
                    exception_count=len(exceptions),
                    exceptions=[str(ex) for ex in exceptions[:3]],
                )
                raise ClaudeProcessError(
                    f"Claude SDK task error: {exceptions[0] if exceptions else e}"
                )

            logger.error(
                "Unexpected error in Claude SDK",
                error=str(e),
                error_type=type(e).__name__,
            )
            raise ClaudeProcessError(f"Unexpected error: {str(e)}")

    async def _handle_stream_message(
        self, message: Message, stream_callback: Callable[[StreamUpdate], None]
    ) -> None:
        """Handle streaming message from claude-agent-sdk."""
        try:
            if isinstance(message, AssistantMessage):
                # Extract content from assistant message
                content = getattr(message, "content", [])
                text_parts = []
                tool_calls = []

                if content and isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolUseBlock):
                            tool_calls.append(
                                {
                                    "name": block.name,
                                    "input": block.input,
                                    "id": block.id,
                                }
                            )
                        elif isinstance(block, TextBlock):
                            text_parts.append(block.text)
                        elif isinstance(block, ThinkingBlock):
                            text_parts.append(block.thinking)

                if text_parts or tool_calls:
                    update = StreamUpdate(
                        type="assistant",
                        content=("\n".join(text_parts) if text_parts else None),
                        tool_calls=tool_calls if tool_calls else None,
                    )
                    await stream_callback(update)
                elif content:
                    # Fallback for non-list content
                    update = StreamUpdate(
                        type="assistant",
                        content=str(content),
                    )
                    await stream_callback(update)

            elif isinstance(message, StreamEvent):
                event = message.event or {}
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            update = StreamUpdate(
                                type="stream_delta",
                                content=text,
                            )
                            await stream_callback(update)

            elif isinstance(message, UserMessage):
                content = getattr(message, "content", "")
                if content:
                    update = StreamUpdate(
                        type="user",
                        content=content,
                    )
                    await stream_callback(update)

        except Exception as e:
            logger.warning("Stream callback failed", error=str(e))

    def _load_mcp_config(self, config_path: Path) -> Dict[str, Any]:
        """Load MCP server configuration from a JSON file.

        The new claude-agent-sdk expects mcp_servers as a dict, not a file path.
        """
        import json

        try:
            with open(config_path) as f:
                config_data = json.load(f)
            return config_data.get("mcpServers", {})
        except (json.JSONDecodeError, OSError) as e:
            logger.error(
                "Failed to load MCP config", path=str(config_path), error=str(e)
            )
            return {}
