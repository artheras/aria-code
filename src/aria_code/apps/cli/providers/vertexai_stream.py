"""Native Google Cloud Vertex AI LLM Provider using google-genai.

google-genai is an optional dependency: most users run Ollama or an
OpenAI-compatible endpoint and should not have to install a Google SDK. That
makes the "it is not installed" path a normal one to land on, so it has to say
what to do rather than leaking a ModuleNotFoundError.
"""

import asyncio
import base64
import json
import os
import re
from typing import AsyncGenerator, Optional

from aria_code.apps.cli.providers.base import (
    LLMDone,
    LLMEvent,
    LLMProvider,
    LLMThinking,
    LLMToken,
    LLMToolCall,
)

_MISSING_SDK_MESSAGE = (
    "Gemini/Vertex AI 需要 google-genai，当前未安装。\n"
    "  安装：pip install google-genai\n"
    "  或改用其他模型：/model  （Ollama 本地模型无需额外依赖）"
)

# How runtime.agent_loop.build_tool_followup ends its results text; what
# follows is guidance, not a copy of the results.
_TOOL_RESULTS_TRAILERS = (
    "\n\n⚠ Tool(s) returned errors:",
    "\n\nAll tools completed successfully.",
)


import logging

logger = logging.getLogger(__name__)

_EMPTY_ROUND_RETRIES = 2

# Waits before re-sending a request Vertex turned away as busy: 429
# RESOURCE_EXHAUSTED (quota or shared capacity) and 500/503/504. The 40-task
# eval run lost inventory-reorder this way, mid-task, to a single 429 that
# ended the turn. Retried only while the round has produced nothing, so no
# text or tool call is ever delivered twice.
_BUSY_BACKOFF = (2, 5, 10, 20, 40)
_BUSY_CODES = frozenset({429, 500, 503, 504})
_BUSY_STATUSES = frozenset({"RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"})
_sleep = asyncio.sleep


def _busy(error) -> bool:
    """True when Vertex refused the request for load, not for anything in it."""
    return (getattr(error, "code", None) in _BUSY_CODES
            or str(getattr(error, "status", "") or "").upper() in _BUSY_STATUSES)


async def _wait(seconds: float, cancel_event: Optional[asyncio.Event]) -> bool:
    """Sleep up to *seconds*; True if the user cancelled meanwhile."""
    if cancel_event is None:
        await _sleep(seconds)
        return False
    waited = 0.0
    while waited < seconds:
        if cancel_event.is_set():
            return True
        step = min(0.5, seconds - waited)
        await _sleep(step)
        waited += step
    return cancel_event.is_set()


def _chunk_text(chunk) -> str:
    """The text parts of a chunk, read without chunk.text.

    chunk.text logs "there are non-text parts in the response: ['function_call']"
    for every chunk that carries a call, which filled the eval logs.
    """
    out = []
    for candidate in (getattr(chunk, "candidates", None) or [])[:1]:
        content = getattr(candidate, "content", None)
        for part in (getattr(content, "parts", None) or []):
            text = getattr(part, "text", None)
            if text and not getattr(part, "thought", False):
                out.append(text)
    if out:
        return "".join(out)
    if not getattr(chunk, "candidates", None):
        try:
            return chunk.text or ""
        except Exception:
            return ""
    return ""


def _chunk_calls(chunk) -> list:
    """The function calls in a chunk, with their ids and thought signatures.

    Signatures are base64 text: the call is recorded in history, and history
    is JSON.
    """
    calls = []
    for candidate in (getattr(chunk, "candidates", None) or [])[:1]:
        content = getattr(candidate, "content", None)
        for part in (getattr(content, "parts", None) or []):
            fc = getattr(part, "function_call", None)
            if not fc:
                continue
            call = {
                "tool": fc.name,
                "params": {k: v for k, v in fc.args.items()} if fc.args else {},
            }
            if getattr(fc, "id", None):
                call["call_id"] = fc.id
            signature = getattr(part, "thought_signature", None)
            if signature:
                call["thought_signature"] = base64.b64encode(signature).decode("ascii")
            calls.append(call)
    if not calls:
        # No call parts to read (a chunk without candidates): take the SDK's
        # own list, which has no signatures.
        for fc in (getattr(chunk, "function_calls", None) or []):
            calls.append({
                "tool": fc.name,
                "params": {k: v for k, v in fc.args.items()} if fc.args else {},
            })
    return calls


class VertexAIProvider(LLMProvider):
    """Native Vertex AI provider using google-genai."""
    
    def __init__(
        self,
        model: str,
        config: Optional[dict] = None,
        system_override: Optional[str] = None,
    ):
        self.model = model
        self.config = config or {}
        self.system_override = system_override
        self._client = None

    def _api_key(self) -> str:
        """Gemini API key from config, falling back to the standard env vars."""
        for value in (
            self.config.get("api_key"),
            self.config.get("gemini_key"),
            os.getenv("GEMINI_API_KEY"),
            os.getenv("GOOGLE_API_KEY"),
        ):
            key = str(value or "").strip()
            if key:
                return key
        return ""

    def _use_vertex(self) -> bool:
        """Decide between Vertex AI (ADC) and the Gemini API-key endpoint.

        ``use_vertexai`` used to default to True unconditionally, so a developer
        holding only a GEMINI_API_KEY got ``genai.Client(vertexai=True)`` and a
        credentials error — Vertex needs application-default credentials and a
        project.  An explicit config value still wins; otherwise pick whichever
        set of credentials is actually present.
        """
        configured = self.config.get("use_vertexai")
        if configured is not None:
            return bool(configured)
        env_flag = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower()
        if env_flag in {"1", "true", "yes", "on"}:
            return True
        if env_flag in {"0", "false", "no", "off"}:
            return False
        has_vertex_creds = bool(
            os.getenv("GOOGLE_CLOUD_PROJECT")
            or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        )
        if has_vertex_creds:
            return True
        # No project/ADC configured: an API key is the only usable path.
        return not self._api_key()

    def _get_client(self):
        if self._client is None:
            from google import genai

            if self._use_vertex():
                project = os.getenv("GOOGLE_CLOUD_PROJECT") or self.config.get("gcp_project")
                # global, not us-central1: the Gemini 3 previews
                # (gemini-3-flash-preview, gemini-3.1-pro-preview) are served
                # only from the global endpoint and 404 in a region, and the
                # global endpoint serves 2.5 as well.
                location = (
                    os.getenv("GOOGLE_CLOUD_LOCATION")
                    or self.config.get("gcp_location")
                    or "global"
                )
                kwargs = {"vertexai": True, "location": location}
                if project:
                    kwargs["project"] = str(project)
                self._client = genai.Client(**kwargs)
            else:
                api_key = self._api_key()
                if not api_key:
                    raise RuntimeError(
                        "Gemini 需要凭据：设置 GEMINI_API_KEY，或配置 Vertex AI "
                        "(GOOGLE_CLOUD_PROJECT + gcloud auth application-default login)。"
                    )
                self._client = genai.Client(api_key=api_key)
        return self._client
        
    def _requires_thought_signatures(self) -> bool:
        """Gemini 3 and later reject a replayed function call without its signature."""
        match = re.search(r"gemini-(\d+)", str(self.model).lower())
        return bool(match) and int(match.group(1)) >= 3

    @staticmethod
    def _append(contents: list, role: str, parts: list, types) -> None:
        # Gemini requires alternating roles: user, model, user, model.
        if contents and contents[-1].role == role:
            contents[-1].parts.extend(parts)
        else:
            contents.append(types.Content(role=role, parts=parts))

    @staticmethod
    def _strip_tool_results_text(text: str) -> str:
        """Drop the text copy of tool results from the loop's follow-up message.

        The follow-up repeats every result as ``## Tool Results ...`` text for
        providers that have no native tool turn. Once the results have gone to
        Gemini as function responses, that copy is not just redundant: it is a
        transcript of tool calls written as prose, and Gemini learns from it to
        write its next calls — and their results — as prose too, which run
        nothing. Only what the loop appends after the results is kept: the
        error / completion guidance and any loop-guard directives.
        """
        cut = max(text.rfind(marker) for marker in _TOOL_RESULTS_TRAILERS)
        return text[cut:].strip() if cut >= 0 else ""

    def _messages_to_contents(self, messages: list):
        # Convert aria chat messages to genai Content objects
        from google.genai import types

        system_instruction = self.system_override or ""
        contents = []
        # Calls from the latest native model turn that still need a
        # function_response, as [name, id] pairs.
        unanswered: list = []
        native_results = False

        def answer_leftovers() -> None:
            # Gemini rejects a turn that leaves any call of the previous model
            # turn unanswered — e.g. when the batch was cut short.
            parts = [
                types.Part(function_response=types.FunctionResponse(
                    name=name, id=call_id, response={"error": "not executed"}))
                for name, call_id in unanswered
            ]
            unanswered.clear()
            if parts:
                self._append(contents, "user", parts, types)

        for msg in messages:
            role = msg.get("role", "user")
            content_str = msg.get("content", "")

            if role == "system":
                if system_instruction:
                    system_instruction += "\n\n" + content_str
                else:
                    system_instruction = content_str
                continue

            genai_role = "user" if role == "user" else "model"

            if role == "assistant" and msg.get("tool_calls"):
                answer_leftovers()
                parts = self._function_call_parts(msg["tool_calls"], types)
                if parts is not None:
                    if str(content_str or "").strip():
                        parts.insert(0, types.Part.from_text(text=content_str))
                    self._append(contents, "model", parts, types)
                    unanswered = [
                        [part.function_call.name, part.function_call.id]
                        for part in parts
                        if part.function_call
                    ]
                    native_results = False
                    continue
                # No usable signatures (history from before they were kept, or
                # from another provider): fall through to the text rendering.

            if role == "tool" and unanswered:
                tool_name = msg.get("name") or ""
                index = next(
                    (i for i, (name, _) in enumerate(unanswered) if name == tool_name),
                    0,
                )
                name, call_id = unanswered.pop(index)
                self._append(contents, "user", [types.Part(
                    function_response=types.FunctionResponse(
                        name=name, id=call_id, response={"result": str(content_str)}),
                )], types)
                native_results = True
                continue

            if role == "tool":
                # No native call to answer: rendered as text instead.
                #
                # Gemini only accepts a function_response that answers a
                # function_call it can see in the preceding model turn.
                # Sending an unanswered function_response made the
                # conversation malformed, and Gemini replied with a single
                # whitespace character and no tool call.
                tool_name = msg.get("name") or "tool"
                text = f"[{tool_name}] {content_str}".strip()
                if not text:
                    continue
                if contents and contents[-1].role == "user":
                    contents[-1].parts.append(types.Part.from_text(text=text))
                else:
                    contents.append(types.Content(
                        role="user", parts=[types.Part.from_text(text=text)]))
                continue

            answer_leftovers()
            if (
                native_results
                and role == "user"
                and isinstance(content_str, str)
                and content_str.lstrip().startswith("## Tool Results")
            ):
                content_str = self._strip_tool_results_text(content_str)
            native_results = False

            # An empty part is worse than no part. When a model answers a turn
            # with nothing but a function call — which Gemini does routinely,
            # and which the agent loop records as an assistant message whose
            # text is "" — this used to send Content(role="model", parts=[""]).
            # Gemini responds to that with a single whitespace character and no
            # tool call, so the second round of every tool-using turn came back
            # as "empty_response" and the task died after one step.
            if not str(content_str or "").strip():
                continue

            # Check if previous message has same role
            # (Gemini requires alternating roles: user, model, user, model)
            if contents and contents[-1].role == genai_role:
                contents[-1].parts.append(types.Part.from_text(text=content_str))
            else:
                contents.append(types.Content(role=genai_role, parts=[types.Part.from_text(text=content_str)]))

        # Gemini refuses a request whose last turn is the model's ("Requests
        # ending with a model turn are not supported"). That happens when the
        # history ends on a tool call that was never answered, or when the
        # message that would have followed it was empty and skipped above.
        answer_leftovers()
        if contents and contents[-1].role == "model":
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text="Continue.")]))
        return contents, system_instruction

    def _function_call_parts(self, tool_calls: list, types) -> Optional[list]:
        """The assistant's recorded calls as function_call parts.

        None when they cannot be replayed natively: Gemini 3 rejects a call
        replayed without the thought signature it was issued with, and calls
        recorded before signatures were kept (or by another provider) have none.
        """
        parts = []
        signed = False
        for tool_call in tool_calls:
            fn = tool_call.get("function", tool_call)
            name = str(fn.get("name") or "")
            if not name:
                continue
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            part = types.Part(function_call=types.FunctionCall(
                name=name, args=dict(args or {}), id=tool_call.get("id") or None))
            signature = tool_call.get("thought_signature")
            if signature:
                part.thought_signature = base64.b64decode(signature)
                signed = True
            parts.append(part)
        if not parts or (self._requires_thought_signatures() and not signed):
            return None
        return parts

    def _schema_from_dict(self, d: dict, types):
        if not d:
            return None
        t = d.get("type", "string").upper()
        if t == "ARRAY":
            items = d.get("items", {})
            return types.Schema(
                type="ARRAY",
                description=d.get("description", ""),
                items=self._schema_from_dict(items, types) if items else types.Schema(type="STRING")
            )
        elif t == "OBJECT":
            props = d.get("properties", {})
            req = d.get("required", [])
            schema_props = {k: self._schema_from_dict(v, types) for k, v in props.items()}
            return types.Schema(
                type="OBJECT",
                description=d.get("description", ""),
                properties=schema_props if schema_props else None,
                required=req if req else None
            )
        else:
            return types.Schema(
                type=t,
                description=d.get("description", "")
            )

    def _tools_to_genai(self, tools: list):
        if not tools:
            return None
        from google.genai import types
        genai_tools = []
        # Vertex rejects the entire request when two declarations share a name
        # ("Duplicate function declaration found: web_fetch"), where
        # OpenAI-compatible backends just take the last one. The registries
        # upstream should not produce duplicates, but this is the boundary
        # where a duplicate becomes a hard 400 for the whole turn, so it is
        # also the boundary that has to be certain.
        seen: set = set()
        for tool in tools:
            func = tool.get("function", tool)
            name = func.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            desc = func.get("description", "")
            
            # Map parameters recursively
            params = func.get("parameters", {})
            schema = self._schema_from_dict(params, types) if params else None
            
            tool_declaration = types.FunctionDeclaration(
                name=name,
                description=desc,
                parameters=schema
            )
            genai_tools.append(types.Tool(function_declarations=[tool_declaration]))
            
        return genai_tools

    async def stream(
        self,
        messages: list,
        tools: list,
        *,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[LLMEvent, None]:
        # These imports must sit INSIDE the try. They were above it, so when
        # google-genai was not installed they raised first and the handler
        # below — the one that explains how to fix it — was unreachable. The
        # user saw a bare "No module named 'google.genai'" and no way forward.
        try:
            from google.genai import types
            from google.genai.errors import APIError

            client = self._get_client()
        except ImportError:
            yield LLMDone(
                response="", provider="vertexai", success=False,
                error=_MISSING_SDK_MESSAGE,
            )
            return
        except Exception as e:
            yield LLMDone(response="", provider="vertexai", success=False, error=str(e))
            return
            
        contents, system_instruction = self._messages_to_contents(messages)
        genai_tools = self._tools_to_genai(tools)
        
        config = types.GenerateContentConfig(
            system_instruction=system_instruction if system_instruction else None,
            tools=genai_tools if genai_tools else None,
            temperature=self.config.get("temperature", 0.7),
        )
        
        try:
            # Gemini sometimes ends a round with no text and no function call:
            # finish_reason MALFORMED_FUNCTION_CALL (it wrote a call it could
            # not emit), or an empty STOP after reading tool results. The round
            # then came back empty, the turn ended as "empty_response", and
            # `aria-code -p` exited 1 a few seconds in: three of nine eval tasks
            # on the first Vertex runs, different ones each time. The same
            # request usually succeeds when sent again, so it is, up to twice,
            # and if it is still empty the finish reason goes into the error.
            full_response = ""
            usage = {}
            tool_calls = []
            finish_reasons: list = []
            busy_tries = 0
            for attempt in range(1 + _EMPTY_ROUND_RETRIES):
                # The SDK sends the request when the stream is first read, so
                # a 429 surfaces inside the loop below, not at the call. A busy
                # refusal is not an empty round: it has its own budget and does
                # not use up one of those retries.
                while True:
                    try:
                        response_stream = await client.aio.models.generate_content_stream(
                            model=self.model,
                            contents=contents,
                            config=config,
                        )
                        async for chunk in response_stream:
                            if cancel_event and cancel_event.is_set():
                                yield LLMDone(response=full_response, provider="vertexai", success=True, cancelled=True)
                                return

                            text = _chunk_text(chunk)
                            if text:
                                full_response += text
                                yield LLMToken(text=text)

                            # Read calls from the parts, not chunk.function_calls:
                            # the thought signature lives on the part, and Gemini 3
                            # refuses the next request if the call is replayed
                            # without it.
                            for call in _chunk_calls(chunk):
                                yield LLMToolCall(
                                    tool=call["tool"], params=call["params"],
                                    call_id=call.get("call_id"),
                                    thought_signature=call.get("thought_signature"),
                                )
                                tool_calls.append(call)

                            for candidate in (getattr(chunk, "candidates", None) or []):
                                reason = getattr(candidate, "finish_reason", None)
                                if reason:
                                    finish_reasons.append(str(getattr(reason, "name", reason)))

                            if chunk.usage_metadata:
                                usage = {
                                    "prompt_tokens": chunk.usage_metadata.prompt_token_count,
                                    "completion_tokens": chunk.usage_metadata.candidates_token_count,
                                }
                        break
                    except APIError as e:
                        if full_response or tool_calls or not _busy(e) or busy_tries >= len(_BUSY_BACKOFF):
                            raise
                        delay = _BUSY_BACKOFF[busy_tries]
                        busy_tries += 1
                        finish_reasons = []
                        logger.info("Vertex is busy (%s %s); retrying in %ss (%d/%d)",
                                    getattr(e, "code", ""), getattr(e, "status", ""),
                                    delay, busy_tries, len(_BUSY_BACKOFF))
                        if await _wait(delay, cancel_event):
                            yield LLMDone(response="", provider="vertexai", success=True, cancelled=True)
                            return
                if full_response.strip() or tool_calls:
                    break
                logger.info("Vertex returned an empty round (finish_reason %s), attempt %d",
                            finish_reasons[-1:] or "none", attempt + 1)
                if attempt < _EMPTY_ROUND_RETRIES:
                    finish_reasons = []

            if not full_response.strip() and not tool_calls:
                reason = finish_reasons[-1] if finish_reasons else "none"
                yield LLMDone(
                    response="", provider="vertexai", success=False,
                    error=f"empty_response (Vertex finish_reason {reason} after "
                          f"{1 + _EMPTY_ROUND_RETRIES} attempts)",
                )
                return

            yield LLMDone(
                response=full_response,
                tool_calls_pending=tool_calls,
                usage=usage,
                provider="vertexai",
                success=True,
                cancelled=False,
            )

        except APIError as e:
            yield LLMDone(response="", provider="vertexai", success=False, error=f"Vertex AI API Error: {e.message}")
        except Exception as e:
            yield LLMDone(response="", provider="vertexai", success=False, error=f"Vertex AI Error: {str(e)}")

