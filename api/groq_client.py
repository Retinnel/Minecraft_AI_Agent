import asyncio
import ast
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from api.console_utils import make_console_safe
from groq import Groq
from openai import OpenAI as OllamaClient
from api.challenges import get_challenge_prompt_context
from api.player_state import get_player_state_prompt_context

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# DeepSeek совместим с API OpenAI. Ключ — платный баланс, поэтому провайдер не по умолчанию:
# подключается выбором модели в llm_config.json, когда квоты Groq не хватает.
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# Separate passive RP polling from interactive chat/tool requests so a slow tip
# request cannot monopolize the workers needed for /api/rp/chat.
_tip_executor = ThreadPoolExecutor(max_workers=1)
_interactive_executor = ThreadPoolExecutor(max_workers=2)


def _strip_thinking(text: str) -> str:
    """Strip <think>...</think> reasoning blocks from LLM output (Qwen3 etc.)."""
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    return cleaned.strip()


def _clean_model_text(text: str) -> str:
    """Keep only the final user-visible answer and trim common reasoning artifacts."""
    cleaned = _strip_thinking(text or "").strip()
    if not cleaned:
        return ""

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    lowered = cleaned.lower()
    for marker in ("final answer:", "final:", "answer:", "ответ:", "итог:", "spoken_response:"):
        idx = lowered.rfind(marker)
        if idx != -1:
            cleaned = cleaned[idx + len(marker) :].strip()
            lowered = cleaned.lower()

    paragraphs = [part.strip() for part in re.split(r"\n{2,}", cleaned) if part.strip()]
    if len(paragraphs) > 1:
        first = paragraphs[0].lower()
        if any(token in first for token in ("let me", "user asked", "пользоват", "нужно", "reasoning", "thinking")):
            cleaned = paragraphs[-1]

    lines = [line.strip(" -*\t") for line in cleaned.splitlines() if line.strip()]
    if len(lines) > 1:
        first = lines[0].lower()
        if any(token in first for token in ("let me", "user asked", "пользоват", "reasoning", "thinking")):
            cleaned = lines[-1]

    return cleaned.strip()


def _extract_json_object(text: str) -> str:
    """Extract a JSON object from raw LLM output."""
    cleaned = _clean_model_text(text)
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end >= start:
        cleaned = cleaned[start : end + 1]

    return cleaned.strip()


def _loads_jsonish_object(text: str) -> Optional[dict]:
    """Parse slightly malformed JSON-ish model output into a dict."""
    candidates = []
    cleaned = _extract_json_object(text)
    if cleaned:
        candidates.append(cleaned)
        candidates.append(re.sub(r",\s*([}\]])", r"\1", cleaned))

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass

        pythonish = candidate
        pythonish = re.sub(r"\btrue\b", "True", pythonish, flags=re.IGNORECASE)
        pythonish = re.sub(r"\bfalse\b", "False", pythonish, flags=re.IGNORECASE)
        pythonish = re.sub(r"\bnull\b", "None", pythonish, flags=re.IGNORECASE)
        try:
            parsed = ast.literal_eval(pythonish)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, SyntaxError):
            continue

    return None


def _render_prompt_template(template: str, **values: str) -> str:
    """Replace only known placeholders and leave literal JSON braces untouched."""
    rendered = template or ""
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value if isinstance(value, str) else str(value))
    return rendered


class GroqClient:
    """Wrapper for LLM API calls (Groq or Ollama) with Minecraft game state context."""

    def __init__(self, llm_config=None):
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
        # Переменная в .env заведена как DEEPSEEK_API; принимается и привычное имя с суффиксом.
        self.deepseek_api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API")
        self.provider_api_key = None
        self.client = None
        self.llm_config = llm_config
        self.model = None
        self.provider = "groq"  # default

        if llm_config:
            self.model = llm_config.get_model_name()
            active = llm_config.get_active_model()
            self.provider = getattr(active, "provider", "groq")
        else:
            self.model = "llama-3.1-8b-instant"

        if self.provider == "ollama":
            try:
                self.client = OllamaClient(base_url=OLLAMA_BASE_URL, api_key="ollama")
                print(make_console_safe(f"[LLM] Using Ollama | model: {self.model} | url: {OLLAMA_BASE_URL}"))
                logger.info(make_console_safe(f"Initialized Ollama client with model: {self.model}"))
            except Exception as e:
                logger.error(f"Failed to initialize Ollama client: {e}")
                self.client = None
        elif self.provider == "openrouter":
            if self.openrouter_api_key:
                try:
                    headers = {"X-Title": os.getenv("OPENROUTER_APP_NAME", "Minecraft AI Assistant")}
                    referer = os.getenv("OPENROUTER_SITE_URL")
                    if referer:
                        headers["HTTP-Referer"] = referer
                    self.provider_api_key = self.openrouter_api_key
                    self.client = OllamaClient(
                        base_url=OPENROUTER_BASE_URL,
                        api_key=self.openrouter_api_key,
                        default_headers=headers,
                    )
                    print(make_console_safe(f"[LLM] Using OpenRouter | model: {self.model} | url: {OPENROUTER_BASE_URL}"))
                    logger.info(make_console_safe(f"Initialized OpenRouter client with model: {self.model}"))
                except Exception as e:
                    logger.error(f"Failed to initialize OpenRouter client: {e}")
                    self.client = None
            else:
                print(make_console_safe("[LLM] WARNING: OPENROUTER_API_KEY not set — OpenRouter unavailable"))
                logger.warning("OPENROUTER_API_KEY not set")
        elif self.provider == "deepseek":
            if self.deepseek_api_key:
                try:
                    self.provider_api_key = self.deepseek_api_key
                    self.client = OllamaClient(base_url=DEEPSEEK_BASE_URL, api_key=self.deepseek_api_key)
                    print(make_console_safe(f"[LLM] Using DeepSeek | model: {self.model} | url: {DEEPSEEK_BASE_URL}"))
                    logger.info(make_console_safe(f"Initialized DeepSeek client with model: {self.model}"))
                except Exception as e:
                    logger.error(f"Failed to initialize DeepSeek client: {e}")
                    self.client = None
            else:
                print(make_console_safe("[LLM] WARNING: DEEPSEEK_API_KEY not set — DeepSeek unavailable"))
                logger.warning("DEEPSEEK_API_KEY not set")
        else:
            if self.groq_api_key:
                try:
                    self.provider_api_key = self.groq_api_key
                    self.client = Groq(api_key=self.groq_api_key)
                    print(make_console_safe(f"[LLM] Using Groq | model: {self.model}"))
                    logger.info(make_console_safe(f"Initialized Groq client with model: {self.model}"))
                except Exception as e:
                    logger.error(f"Failed to initialize Groq client: {e}")
                    self.client = None
            else:
                print(make_console_safe("[LLM] WARNING: GROQ_API_KEY not set — Groq unavailable"))
                logger.warning("GROQ_API_KEY not set")

    def get_source_name(self) -> str:
        """Return a stable source/provider name for logs and API responses."""
        return self.provider

    def is_available(self) -> bool:
        """Check if LLM API is configured and ready."""
        if self.provider == "ollama":
            return self.client is not None
        return self.client is not None and self.provider_api_key is not None

    def _get_max_tokens(self) -> int:
        """Get max tokens from config or default."""
        if self.llm_config:
            return self.llm_config.get_parameter("max_tokens", 100)
        return 100

    def _get_temperature(self) -> float:
        """Get temperature from config or default."""
        if self.llm_config:
            return self.llm_config.get_parameter("temperature", 0.7)
        return 0.7

    def _build_api_params(self) -> dict:
        """Build API parameters based on model config."""
        params = {
            "model": self.model,
            "max_tokens": self._get_max_tokens(),
            "messages": [],  # Will be set by caller
        }
        
        # Add temperature if it exists
        if self.llm_config and self.llm_config.has_parameter("temperature"):
            params["temperature"] = self._get_temperature()
        
        # Add reasoning_effort only for providers known to support it in this app.
        if self.provider in {"groq", "ollama"} and self.llm_config and self.llm_config.has_parameter("reasoning_effort"):
            params["reasoning_effort"] = self.llm_config.get_parameter("reasoning_effort")
        
        return params

    def _create_completion(self, api_params: dict):
        """Create a completion and retry Ollama/Qwen calls that exhaust budget on reasoning."""
        completion = self.client.chat.completions.create(**api_params)

        if self.provider != "ollama":
            return completion

        try:
            choice = completion.choices[0]
            message = choice.message
            content = getattr(message, "content", "") or ""
            reasoning = getattr(message, "reasoning", None)
            finish_reason = getattr(choice, "finish_reason", None)
        except (AttributeError, IndexError):
            return completion

        if content.strip():
            return completion

        if not reasoning:
            return completion

        if finish_reason not in {"length", None}:
            return completion

        retry_max_tokens = max(int(api_params.get("max_tokens", 100)) * 4, 800)
        if retry_max_tokens <= int(api_params.get("max_tokens", 100)):
            return completion

        retry_params = dict(api_params)
        retry_params["max_tokens"] = retry_max_tokens
        logger.info(
            "Retrying Ollama completion with higher max_tokens=%s because content was empty and reasoning consumed the budget",
            retry_max_tokens,
        )
        return self.client.chat.completions.create(**retry_params)

    def generate_tip(self, recent_logs: list) -> Optional[str]:
        """
        Generate a role-play response based on recent game state logs.
        BLOCKING - should be called from thread pool!

        Args:
            recent_logs: List of recent LogEntry objects (last 5 events)

        Returns:
            Short RP response string or None if API call fails
        """
        if not self.is_available():
            return None

        # Format recent logs into a context string
        context = self._format_logs_context(recent_logs)

        # Get prompt template from config
        if self.llm_config:
            prompt_template = self.llm_config.get_state_prompt_template()
            prompt = _render_prompt_template(prompt_template, context=context)
        else:
            prompt = f"""You are a role-playing Minecraft game companion. Based on the player's recent game state, provide a SHORT (1-2 sentences) humorous or sarcastic response in Russian.

Recent game events:
{context}

Rules:
- Keep it SHORT (max 1-2 sentences)
- Be SPECIFIC to what's happening
- Use Russian language

Provide only the role-play response, nothing else."""

        try:
            # Build API parameters based on model configuration
            api_params = self._build_api_params()
            api_params["messages"] = [{"role": "user", "content": prompt}]
            
            message = self._create_completion(api_params)
            tip = _clean_model_text(message.choices[0].message.content)
            return tip if tip else None
        except Exception as e:
            logger.error(f"{self.get_source_name()} tip API error: {e}")
            return None

    def generate_chat_response(
        self,
        player_message: str,
        recent_logs: list,
        conversation_history: Optional[list] = None,
    ) -> Optional[str]:
        """
        Generate a role-play response to player's chat message.
        BLOCKING - should be called from thread pool!

        Args:
            player_message: The player's chat message
            recent_logs: List of recent LogEntry objects (last 5 events)

        Returns:
            RP response to player's message or None if API call fails
        """
        if not self.is_available():
            return None

        context = self._format_logs_context(recent_logs)
        conversation_context = self._format_conversation_history(conversation_history)

        # Get prompt template from config
        if self.llm_config:
            prompt_template = self.llm_config.get_chat_prompt_template()
            prompt = _render_prompt_template(
                prompt_template,
                player_message=player_message,
                context=context,
                conversation_history=conversation_context,
            )
        else:
            prompt = f"""You are a role-playing Minecraft game companion talking to a player.

Recent conversation:
{conversation_context}

The player just said:
"{player_message}"

Current game context:
{context}

Respond in character as a helpful, humorous companion in Russian. Keep it SHORT (1 sentence max).
Respond only with the RP response, nothing else."""

        try:
            # Build API parameters based on model configuration
            api_params = self._build_api_params()
            api_params["messages"] = [{"role": "user", "content": prompt}]
            
            message = self._create_completion(api_params)
            response = _clean_model_text(message.choices[0].message.content)
            return response if response else None
        except Exception as e:
            logger.error(f"{self.get_source_name()} chat API error: {e}")
            return None

    def _format_conversation_history(self, conversation_history: Optional[list]) -> str:
        if not conversation_history:
            return "Нет недавнего диалога."

        lines = []
        for entry in conversation_history[-10:]:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("role") or "").strip().lower()
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            if role == "user":
                label = "Игрок"
            elif role == "assistant":
                label = "Ассистент"
            elif role == "tool":
                label = "Tool"
            elif role == "tool_result":
                label = "Результат tool"
            else:
                label = "Контекст"
            lines.append(f"{label}: {content}")

        return "\n".join(lines) if lines else "Нет недавнего диалога."

    def _format_available_tools(self, tools: Optional[list]) -> str:
        if not tools:
            return "Нет доступных tools."

        lines = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            description = str(tool.get("description") or "").strip()
            arguments = tool.get("arguments") or {}
            if not name:
                continue

            argument_lines = []
            if isinstance(arguments, dict):
                for arg_name, arg_description in arguments.items():
                    argument_lines.append(f"  - {arg_name}: {arg_description}")

            lines.append(f"- {name}: {description}")
            lines.extend(argument_lines)

        return "\n".join(lines) if lines else "Нет доступных tools."

    def generate_chat_action(
        self,
        player_message: str,
        recent_logs: list,
        conversation_history: Optional[list] = None,
    ) -> Optional[dict]:
        """
        Generate a structured response for either normal chat or a validated action request.
        BLOCKING - should be called from thread pool!
        """
        if not self.is_available():
            return None

        context = self._format_logs_context(recent_logs)
        conversation_context = self._format_conversation_history(conversation_history)

        prompt_template = self.llm_config.get_action_prompt_template() if self.llm_config else None
        if prompt_template:
            prompt = _render_prompt_template(
                prompt_template,
                player_message=player_message,
                context=context,
                conversation_history=conversation_context,
            )
        else:
            prompt = f"""You process Minecraft player chat and must decide whether the message is a normal role-play chat or a tool invocation.

Recent conversation:
{conversation_context}

Player message:
"{player_message}"

Current game context:
{context}

Return ONLY a valid JSON object with this exact schema:
{{
  "mode": "chat" | "action",
  "spoken_response": "short Russian reply for the player",
  "tool_name": "give_item" | null,
  "arguments": {{
    "item_query": "player wording for the requested item",
    "count": 1
  }} | null,
  "should_execute": true | false,
  "error": null | "short Russian error"
}}

Rules:
- Use mode "action" only when the player is asking to receive an item, even in soft or indirect phrasing.
- The only supported tool is "give_item".
- Put the player's item wording into arguments.item_query instead of inventing a command.
- Use count from 1 to 64.
- The spoken_response must contain only the final visible reply in Russian, with no reasoning.
- For normal conversation, use mode "chat", set should_execute to false, and leave tool_name/arguments null.
- Do not output markdown, explanations, or extra text outside JSON."""

        try:
            api_params = self._build_api_params()
            if api_params.get("max_tokens", 100) < 220:
                api_params["max_tokens"] = 220
            api_params["temperature"] = 0.0
            api_params["messages"] = [{"role": "user", "content": prompt}]

            message = self._create_completion(api_params)
            return _loads_jsonish_object(message.choices[0].message.content)
        except Exception as e:
            logger.error(f"{self.get_source_name()} chat action API error: {e}")
            return None

    def generate_tool_decision(
        self,
        player_message: str,
        recent_logs: list,
        conversation_history: Optional[list] = None,
        available_tools: Optional[list] = None,
    ) -> Optional[dict]:
        if not self.is_available():
            return None

        context = self._format_logs_context(recent_logs)
        conversation_context = self._format_conversation_history(conversation_history)
        tools_context = self._format_available_tools(available_tools)

        prompt_template = self.llm_config.get_action_prompt_template() if self.llm_config else None
        if prompt_template:
            prompt = _render_prompt_template(
                prompt_template,
                player_message=player_message,
                context=context,
                conversation_history=conversation_context,
                available_tools=tools_context,
            )
        else:
            prompt = f"""You are an assistant with tools.

Available tools:
{tools_context}

Recent conversation:
{conversation_context}

Player message:
"{player_message}"

Current game context:
{context}

Return ONLY valid JSON:
{{
  "assistant_response": null | "short final Russian reply if no tool is needed",
  "tool_call": null | {{
    "name": "one of the available tool names",
    "arguments": {{
      "key": "value"
    }}
  }}
}}"""

        try:
            api_params = self._build_api_params()
            if api_params.get("max_tokens", 100) < 260:
                api_params["max_tokens"] = 260
            api_params["temperature"] = 0.0
            api_params["messages"] = [{"role": "user", "content": prompt}]

            message = self._create_completion(api_params)
            return _loads_jsonish_object(message.choices[0].message.content)
        except Exception as e:
            logger.error(f"{self.get_source_name()} tool decision API error: {e}")
            return None

    def generate_tool_followup(
        self,
        player_message: str,
        recent_logs: list,
        conversation_history: Optional[list] = None,
        tool_result: Optional[dict] = None,
    ) -> Optional[str]:
        if not self.is_available():
            return None

        context = self._format_logs_context(recent_logs)
        conversation_context = self._format_conversation_history(conversation_history)
        tool_result_text = json.dumps(tool_result or {}, ensure_ascii=False, indent=2)
        prompt_template = self.llm_config.get_tool_result_prompt_template() if self.llm_config else None

        if prompt_template:
            prompt = _render_prompt_template(
                prompt_template,
                player_message=player_message,
                context=context,
                conversation_history=conversation_context,
                tool_result=tool_result_text,
            )
        else:
            prompt = f"""A tool has already been executed.

Recent conversation:
{conversation_context}

Player message:
"{player_message}"

Current game context:
{context}

Tool result:
{tool_result_text}

Return only the final short Russian reply for the player."""

        try:
            api_params = self._build_api_params()
            api_params["messages"] = [{"role": "user", "content": prompt}]

            message = self._create_completion(api_params)
            response = _clean_model_text(message.choices[0].message.content)
            return response if response else None
        except Exception as e:
            logger.error(f"{self.get_source_name()} tool followup API error: {e}")
            return None

    async def generate_tip_async(self, recent_logs: list) -> Optional[str]:
        """
        Async wrapper for generate_tip that doesn't block the event loop.
        
        Args:
            recent_logs: List of recent LogEntry objects
            
        Returns:
            Short RP response string or None if API call fails
        """
        try:
            # Run blocking call in thread pool
            loop = asyncio.get_event_loop()
            tip = await loop.run_in_executor(_tip_executor, self.generate_tip, recent_logs)
            return tip
        except Exception as e:
            logger.error(f"Async generate_tip error: {e}")
            return None

    async def generate_chat_response_async(
        self,
        player_message: str,
        recent_logs: list,
        conversation_history: Optional[list] = None,
    ) -> Optional[str]:
        """
        Async wrapper for generate_chat_response that doesn't block the event loop.
        
        Args:
            player_message: The player's chat message
            recent_logs: List of recent LogEntry objects
            
        Returns:
            RP response or None if API call fails
        """
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                _interactive_executor,
                self.generate_chat_response,
                player_message,
                recent_logs,
                conversation_history,
            )
            return response
        except Exception as e:
            logger.error(f"Async generate_chat_response error: {e}")
            return None

    async def generate_chat_action_async(
        self,
        player_message: str,
        recent_logs: list,
        conversation_history: Optional[list] = None,
    ) -> Optional[dict]:
        """Async wrapper for generate_chat_action."""
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                _interactive_executor,
                self.generate_chat_action,
                player_message,
                recent_logs,
                conversation_history,
            )
            return response
        except Exception as e:
            logger.error(f"Async generate_chat_action error: {e}")
            return None

    async def generate_tool_decision_async(
        self,
        player_message: str,
        recent_logs: list,
        conversation_history: Optional[list] = None,
        available_tools: Optional[list] = None,
    ) -> Optional[dict]:
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                _interactive_executor,
                self.generate_tool_decision,
                player_message,
                recent_logs,
                conversation_history,
                available_tools,
            )
            return response
        except Exception as e:
            logger.error(f"Async generate_tool_decision error: {e}")
            return None

    async def generate_tool_followup_async(
        self,
        player_message: str,
        recent_logs: list,
        conversation_history: Optional[list] = None,
        tool_result: Optional[dict] = None,
    ) -> Optional[str]:
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                _interactive_executor,
                self.generate_tool_followup,
                player_message,
                recent_logs,
                conversation_history,
                tool_result,
            )
            return response
        except Exception as e:
            logger.error(f"Async generate_tool_followup error: {e}")
            return None

    def _format_logs_context(self, recent_logs: list) -> str:
        """Format recent logs into readable context for the LLM."""
        player_state_context = get_player_state_prompt_context()
        challenge_context = get_challenge_prompt_context()
        if not recent_logs:
            blocks = ["No recent events recorded."]
            if player_state_context and player_state_context != "No tracked player state.":
                blocks.append(f"Player state:\n{player_state_context}")
            if challenge_context and challenge_context != "No active challenge.":
                blocks.append(f"Challenge state:\n{challenge_context}")
            return "\n\n".join(blocks)

        lines = []
        for log in recent_logs:
            event_type = getattr(log, "event_type", None)
            event_data = getattr(log, "event_data", None)
            player_health = getattr(log, "player_health", None)
            threats = getattr(log, "threats_detected", None)

            # Build event description
            if event_type:
                lines.append(f"- Event: {event_type}")
                if event_data:
                    lines.append(f"  Details: {event_data}")

            # Add state info
            if player_health is not None:
                lines.append(f"- Health: {player_health}")
            if threats:
                lines.append(f"- Threats detected: {len(threats)} active")

        if player_state_context and player_state_context != "No tracked player state.":
            lines.append("")
            lines.append("Player state:")
            lines.append(player_state_context)

        if challenge_context and challenge_context != "No active challenge.":
            lines.append("")
            lines.append("Challenge state:")
            lines.append(challenge_context)

        return "\n".join(lines) if lines else "No recent events recorded."

    def generate_analytics(self, session_summary: dict) -> Optional[str]:
        """
        Generate post-match tactical analysis based on session summary.
        BLOCKING - should be called from thread pool!

        Args:
            session_summary: Dict containing aggregated session stats

        Returns:
            Detailed markdown response with tactical analysis or None if fails
        """
        if not self.is_available():
            return None

        prompt = f"""You are an elite eSports coach and Minecraft tactical analyst.
The player has just finished a game session. Here is their performance summary:

Total Logs Analyzed: {session_summary.get('total_logs', 0)}
Deaths: {session_summary.get('deaths', 0)}
Low Health Warnings: {session_summary.get('low_health_warnings', 0)}
Hostile Encounters: {session_summary.get('hostile_encounters', 0)}

Significant events timeline:
{chr(10).join(session_summary.get('timeline', []))}

Provide a highly engaging, structured tactical review in Russian.
Include:
1. Оценка выживаемости (Survival Rating)
2. Главные ошибки (Critical Mistakes)
3. Советы на следующую сессию (Tips for next session)
Format using markdown for readability."""

        try:
            api_params = self._build_api_params()
            # We bypass max_tokens here for a full report if it's set too low for tips
            original_max_tokens = api_params.get("max_tokens", 100)
            if original_max_tokens < 500:
                api_params["max_tokens"] = 800

            api_params["messages"] = [{"role": "user", "content": prompt}]
            
            message = self._create_completion(api_params)
            response = _clean_model_text(message.choices[0].message.content)
            return response if response else None
        except Exception as e:
            logger.error(f"{self.get_source_name()} analytics API error: {e}")
            return None

    async def generate_analytics_async(self, session_summary: dict) -> Optional[str]:
        """Async wrapper for generate_analytics."""
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(_interactive_executor, self.generate_analytics, session_summary)
            return response
        except Exception as e:
            logger.error(f"Async generate_analytics error: {e}")
            return None

    def search_crafting_recipe(self, query: str) -> Optional[dict]:
        """
        Uses LLM to search for a Minecraft crafting recipe and returns structured JSON representing a 3x3 grid.
        BLOCKING - should be called from thread pool!
        """
        if not self.is_available():
            return None

        prompt = f"""You are a Minecraft 1.20+ crafting expert. The user wants to craft: "{query}".

IMPORTANT RULES:
1. Respond ONLY with valid JSON, NO markdown wrapping (no ```json```).
2. The grid MUST be exactly 3x3 (9 cells total).
3. Use exact ingredient names in Russian (e.g., "доска", "палка", "железный слиток").
4. Empty cells MUST be exactly "пусто".
5. Each ingredient name should be SHORT and accurate.

If the item cannot be crafted in vanilla survival (Bedrock, Eggs, etc.), set grid all "пусто" and write "Невозможно скрафтить в выживании" in description.

Return JSON in this format ONLY:
{{
  "name": "Название предмета на русском",
  "description": "Короткое описание и применение (одна строка)",
  "grid": [
    ["ингредиент", "ингредиент", "пусто"],
    ["ингредиент", "пусто", "пусто"],
    ["пусто", "пусто", "пусто"]
  ]
}}"""

        try:
            api_params = self._build_api_params()
            api_params["messages"] = [{"role": "user", "content": prompt}]
            # Disable temp for deterministic recipes
            api_params["temperature"] = 0.0
            # Increase max_tokens for detailed recipes
            api_params["max_tokens"] = 300
            
            message = self._create_completion(api_params)
            result = _loads_jsonish_object(message.choices[0].message.content)
            
            # Validate result
            if result and isinstance(result, dict):
                # Ensure grid is properly formatted
                if "grid" in result and isinstance(result["grid"], list) and len(result["grid"]) == 3:
                    # Validate each row has 3 items
                    for row in result["grid"]:
                        if not isinstance(row, list) or len(row) != 3:
                            logger.warning(f"Invalid grid format for recipe: {query}")
                            return None
                    return result
            
            return None
        except Exception as e:
            logger.error(f"{self.get_source_name()} wiki API error: {e}")
            return None

    async def search_crafting_recipe_async(self, query: str) -> Optional[dict]:
        """Async wrapper for search_crafting_recipe."""
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(_interactive_executor, self.search_crafting_recipe, query)
            return response
        except Exception as e:
            logger.error(f"Async search_crafting_recipe error: {e}")
            return None
