import unittest
from unittest.mock import patch

from api.groq_client import GroqClient, _clean_model_text, _loads_jsonish_object
from api.player_state import reset_player_state
from models.llm_config import LLMConfig, ModelConfig, PromptsConfig


class _FakeResponse:
    def __init__(self, content: str, *, reasoning: str | None = None, finish_reason: str = "stop"):
        self.choices = [
            type(
                "Choice",
                (),
                {
                    "finish_reason": finish_reason,
                    "message": type("Message", (), {"content": content, "reasoning": reasoning})(),
                },
            )()
        ]


class _FakeClient:
    def __init__(self, content: str):
        self._content = content
        self.chat = type(
            "Chat",
            (),
            {
                "completions": type(
                    "Completions",
                    (),
                    {"create": lambda inner_self, **kwargs: _FakeResponse(self._content)},
                )()
            },
        )()


class _SequencedFakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self._calls = 0

        def _create(**kwargs):
            response = self._responses[min(self._calls, len(self._responses) - 1)]
            self._calls += 1
            return response

        self.chat = type(
            "Chat",
            (),
            {"completions": type("Completions", (), {"create": staticmethod(_create)})()},
        )()


class _CapturingFakeClient:
    def __init__(self, content: str):
        self._content = content
        self.last_messages = None

        def _create(**kwargs):
            self.last_messages = kwargs.get("messages")
            return _FakeResponse(self._content)

        self.chat = type(
            "Chat",
            (),
            {"completions": type("Completions", (), {"create": staticmethod(_create)})()},
        )()


def _build_config(provider: str = "ollama") -> LLMConfig:
    model_name = "qwen3:8b"
    if provider == "groq":
        model_name = "llama-3.1-8b-instant"
    elif provider == "openrouter":
        model_name = "inclusionai/ling-2.6-1t:free"
    elif provider == "deepseek":
        model_name = "deepseek-chat"

    return LLMConfig(
        model_type="test-model",
        models={
            "test-model": ModelConfig(
                name=model_name,
                provider=provider,
                parameters={"max_tokens": 100, "temperature": 0.1},
                description="test model",
            )
        },
        prompts=PromptsConfig(
            state_based="state {context}",
            chat_based="chat {conversation_history} {player_message} {context}",
            action_based='tool prompt {available_tools} {conversation_history} {player_message} {context} {"assistant_response": null, "tool_call": null}',
            tool_result_based="tool result {conversation_history} {player_message} {context} {tool_result}",
        ),
    )


class GroqClientTests(unittest.TestCase):
    def setUp(self):
        reset_player_state()

    def test_loads_jsonish_object_supports_markdown_and_single_quotes(self):
        parsed = _loads_jsonish_object(
            """```json
            {
              'mode': 'action',
              'spoken_response': 'Выдаю меч.',
              'action_type': 'give',
              'item_id': 'minecraft:diamond_sword',
              'count': 1,
              'execute': true,
            }
            ```"""
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["mode"], "action")
        self.assertEqual(parsed["item_id"], "minecraft:diamond_sword")

    def test_ollama_provider_is_selected_from_config(self):
        fake_client = _FakeClient('{"mode":"chat","spoken_response":"ok","action_type":null,"item_id":null,"count":1,"execute":false,"error":null}')
        with patch("api.groq_client.OllamaClient", return_value=fake_client):
            client = GroqClient(llm_config=_build_config(provider="ollama"))

        self.assertEqual(client.provider, "ollama")
        self.assertEqual(client.get_source_name(), "ollama")
        self.assertTrue(client.is_available())

    def test_groq_provider_is_selected_from_config(self):
        fake_client = _FakeClient('{"mode":"chat","spoken_response":"ok","action_type":null,"item_id":null,"count":1,"execute":false,"error":null}')
        with patch("api.groq_client.Groq", return_value=fake_client), patch("api.groq_client.os.getenv", return_value="test-key"):
            client = GroqClient(llm_config=_build_config(provider="groq"))

        self.assertEqual(client.provider, "groq")
        self.assertEqual(client.get_source_name(), "groq")
        self.assertTrue(client.is_available())

    def test_openrouter_provider_is_selected_from_config(self):
        fake_client = _FakeClient('{"mode":"chat","spoken_response":"ok","action_type":null,"item_id":null,"count":1,"execute":false,"error":null}')

        def _getenv(key, default=None):
            if key == "OPENROUTER_API_KEY":
                return "openrouter-test-key"
            return default

        with patch("api.groq_client.OllamaClient", return_value=fake_client), patch("api.groq_client.os.getenv", side_effect=_getenv):
            client = GroqClient(llm_config=_build_config(provider="openrouter"))

        self.assertEqual(client.provider, "openrouter")
        self.assertEqual(client.model, "inclusionai/ling-2.6-1t:free")
        self.assertEqual(client.get_source_name(), "openrouter")
        self.assertTrue(client.is_available())

    def test_deepseek_provider_reads_the_short_env_name(self):
        # В .env ключ заведён как DEEPSEEK_API; клиент принимает его наравне с DEEPSEEK_API_KEY.
        fake_client = _FakeClient('{"mode":"chat","spoken_response":"ok","action_type":null,"item_id":null,"count":1,"execute":false,"error":null}')

        def _getenv(key, default=None):
            if key == "DEEPSEEK_API":
                return "deepseek-test-key"
            return default

        with patch("api.groq_client.OllamaClient", return_value=fake_client), patch("api.groq_client.os.getenv", side_effect=_getenv):
            client = GroqClient(llm_config=_build_config(provider="deepseek"))

        self.assertEqual(client.provider, "deepseek")
        self.assertEqual(client.model, "deepseek-chat")
        self.assertTrue(client.is_available())

    def test_deepseek_without_key_is_unavailable(self):
        with patch("api.groq_client.os.getenv", return_value=None):
            client = GroqClient(llm_config=_build_config(provider="deepseek"))
        self.assertFalse(client.is_available())

    def test_generate_chat_action_parses_mock_llm_output_for_russian_command(self):
        fake_client = _FakeClient(
            """{
                "mode": "action",
                "spoken_response": "Выдаю яблоки.",
                "tool_name": "give_item",
                "arguments": {"item_query": "яблоки", "count": 3},
                "should_execute": true,
                "error": null
            }"""
        )
        with patch("api.groq_client.OllamaClient", return_value=fake_client):
            client = GroqClient(llm_config=_build_config(provider="ollama"))

        result = client.generate_chat_action("дай 3 яблока", [])
        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "action")
        self.assertEqual(result["tool_name"], "give_item")
        self.assertEqual(result["arguments"]["item_query"], "яблоки")
        self.assertEqual(result["arguments"]["count"], 3)

    def test_generate_chat_action_returns_none_for_unparseable_output(self):
        fake_client = _FakeClient("not a json answer at all")
        with patch("api.groq_client.OllamaClient", return_value=fake_client):
            client = GroqClient(llm_config=_build_config(provider="ollama"))

        result = client.generate_chat_action("дай меч", [])
        self.assertIsNone(result)

    def test_generate_chat_action_parses_mock_groq_output(self):
        fake_client = _FakeClient(
            '{"mode":"action","spoken_response":"Выдаю меч.","tool_name":"give_item","arguments":{"item_query":"меч","count":1},"should_execute":true,"error":null}'
        )
        with patch("api.groq_client.Groq", return_value=fake_client), patch("api.groq_client.os.getenv", return_value="test-key"):
            client = GroqClient(llm_config=_build_config(provider="groq"))

        result = client.generate_chat_action("give me a sword", [])
        self.assertIsNotNone(result)
        self.assertEqual(result["tool_name"], "give_item")
        self.assertEqual(result["arguments"]["item_query"], "меч")

    def test_generate_chat_action_retries_ollama_when_reasoning_consumes_budget(self):
        fake_client = _SequencedFakeClient(
            [
                _FakeResponse("", reasoning="long chain of thought", finish_reason="length"),
                _FakeResponse(
                    '{"mode":"action","spoken_response":"Выдаю шалкеровый ящик.","tool_name":"give_item","arguments":{"item_query":"шалкеровый ящик","count":1},"should_execute":true,"error":null}'
                ),
            ]
        )
        with patch("api.groq_client.OllamaClient", return_value=fake_client):
            client = GroqClient(llm_config=_build_config(provider="ollama"))

        result = client.generate_chat_action("дай мне шалкеровый ящик", [])
        self.assertIsNotNone(result)
        self.assertEqual(result["tool_name"], "give_item")
        self.assertEqual(fake_client._calls, 2)

    def test_clean_model_text_strips_reasoning_and_keeps_final_answer(self):
        text = "<think>long hidden reasoning</think>\n\nОтвет: Конечно, держи пузырёк."
        self.assertEqual(_clean_model_text(text), "Конечно, держи пузырёк.")

    def test_generate_chat_action_includes_recent_conversation_in_prompt(self):
        fake_client = _CapturingFakeClient(
            '{"mode":"chat","spoken_response":"Помню.","tool_name":null,"arguments":null,"should_execute":false,"error":null}'
        )
        with patch("api.groq_client.OllamaClient", return_value=fake_client):
            client = GroqClient(llm_config=_build_config(provider="ollama"))

        result = client.generate_chat_action(
            "что я сказал?",
            [],
            [{"role": "user", "content": "привет"}, {"role": "assistant", "content": "И тебе привет."}],
        )
        self.assertIsNotNone(result)
        prompt = fake_client.last_messages[0]["content"]
        self.assertIn("привет", prompt.lower())
        self.assertIn("и тебе привет", prompt.lower())

    def test_generate_tool_decision_supports_literal_json_in_prompt_template(self):
        fake_client = _CapturingFakeClient(
            '{"assistant_response":null,"tool_call":{"name":"give_item","arguments":{"item_query":"уголь","count":1}}}'
        )
        with patch("api.groq_client.OllamaClient", return_value=fake_client):
            client = GroqClient(llm_config=_build_config(provider="ollama"))

        result = client.generate_tool_decision(
            "и ещё уголь",
            [],
            [{"role": "assistant", "content": "Держи меч."}],
            [{"name": "give_item", "description": "Give an item", "arguments": {"item_query": "query", "count": "1-64"}}],
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["tool_call"]["name"], "give_item")
        prompt = fake_client.last_messages[0]["content"]
        self.assertIn('"assistant_response"', prompt)
        self.assertIn("give_item", prompt)
