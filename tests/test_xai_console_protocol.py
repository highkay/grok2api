import unittest

import orjson

from app.dataplane.reverse.protocol.xai_console import (
    ConsoleStreamAdapter,
    build_console_input,
    classify_console_sse_line,
    convert_openai_tool_choice,
    convert_openai_tools_to_console,
    extract_console_search_sources,
    inject_web_search_tool,
)


class ConsoleProtocolTests(unittest.TestCase):
    def test_build_console_input_preserves_roles_images_and_tool_turns(self):
        messages = [
            {"role": "system", "content": "Be concise."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect this"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://example.test/a.png",
                            "detail": "high",
                        },
                    },
                ],
            },
            {
                "role": "assistant",
                "content": "Calling tool",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": "{\"q\":\"x\"}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "tool output",
            },
        ]

        input_array, instructions = build_console_input(messages)

        self.assertEqual(instructions, "Be concise.")
        self.assertEqual(input_array[0]["role"], "user")
        self.assertEqual(
            input_array[0]["content"],
            [
                {"type": "input_text", "text": "Inspect this"},
                {
                    "type": "input_image",
                    "image_url": "https://example.test/a.png",
                    "detail": "high",
                },
            ],
        )
        self.assertEqual(input_array[1]["type"], "function_call")
        self.assertEqual(input_array[1]["call_id"], "call_1")
        self.assertEqual(input_array[1]["name"], "lookup")
        self.assertEqual(input_array[2]["role"], "assistant")
        self.assertEqual(input_array[2]["content"][0]["text"], "Calling tool")
        self.assertEqual(input_array[3]["type"], "function_call_output")
        self.assertEqual(input_array[3]["call_id"], "call_1")

    def test_tool_conversion_and_web_search_injection_are_idempotent(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Lookup something",
                    "parameters": {"type": "object"},
                },
            }
        ]

        converted = convert_openai_tools_to_console(tools)
        self.assertEqual(
            converted,
            [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Lookup something",
                    "parameters": {"type": "object"},
                }
            ],
        )
        self.assertEqual(
            convert_openai_tool_choice(
                {"type": "function", "function": {"name": "lookup"}}
            ),
            {"type": "function", "name": "lookup"},
        )

        with_search = inject_web_search_tool(converted)
        self.assertEqual(with_search[-1], {"type": "web_search"})
        self.assertEqual(inject_web_search_tool(with_search), with_search)

    def test_extract_console_search_sources_dedupes_web_calls_and_annotations(self):
        response_json = {
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "type": "search",
                        "sources": [
                            {"url": "https://example.test/a", "title": "A"},
                            {"url": "https://example.test/a", "title": "A duplicate"},
                        ],
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "answer",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://example.test/a",
                                    "title": "A",
                                },
                                {
                                    "type": "url_citation",
                                    "url": "https://example.test/b",
                                    "title": "https://example.test/b",
                                },
                            ],
                        }
                    ],
                },
            ]
        }

        self.assertEqual(
            extract_console_search_sources(response_json),
            [
                {"url": "https://example.test/a", "title": "A"},
                {"url": "https://example.test/b", "title": ""},
            ],
        )

    def test_stream_adapter_tracks_text_tool_annotations_and_usage(self):
        adapter = ConsoleStreamAdapter()

        self.assertEqual(
            classify_console_sse_line("event: response.output_text.delta"),
            ("event", "response.output_text.delta"),
        )

        adapter.feed_event("response.output_text.delta")
        self.assertEqual(
            adapter.feed_data(orjson.dumps({"delta": "hello"}).decode()),
            {"kind": "text", "content": "hello"},
        )

        adapter.feed_event("response.output_item.added")
        start = adapter.feed_data(
            orjson.dumps(
                {
                    "item": {
                        "id": "fc_1",
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "lookup",
                    }
                }
            ).decode()
        )
        self.assertEqual(start["kind"], "tool_call_start")
        self.assertEqual(start["index"], 0)

        adapter.feed_event("response.function_call_arguments.delta")
        self.assertEqual(
            adapter.feed_data(
                orjson.dumps({"item_id": "fc_1", "delta": "{\"q\""}).decode()
            ),
            {"kind": "tool_call_args", "index": 0, "delta": "{\"q\""},
        )
        adapter.feed_event("response.function_call_arguments.done")
        self.assertEqual(
            adapter.feed_data(
                orjson.dumps({"item_id": "fc_1", "arguments": "{\"q\":\"x\"}"}).decode()
            ),
            {"kind": "tool_call_done", "index": 0},
        )
        self.assertEqual(
            adapter.tool_calls[0]["function"]["arguments"],
            "{\"q\":\"x\"}",
        )

        adapter.feed_event("response.output_text.annotation.added")
        annotation = adapter.feed_data(
            orjson.dumps(
                {
                    "annotation": {
                        "type": "url_citation",
                        "url": "https://example.test/a",
                        "title": "https://example.test/a",
                    }
                }
            ).decode()
        )
        self.assertEqual(annotation["kind"], "annotation")
        self.assertEqual(adapter.search_sources, [{"url": "https://example.test/a", "title": ""}])

        adapter.feed_event("response.completed")
        self.assertEqual(
            adapter.feed_data(
                orjson.dumps(
                    {
                        "response": {
                            "usage": {
                                "input_tokens": 3,
                                "output_tokens": 5,
                                "total_tokens": 8,
                                "output_tokens_details": {"reasoning_tokens": 2},
                            }
                        }
                    }
                ).decode()
            ),
            {"kind": "done"},
        )
        self.assertEqual(
            adapter.usage,
            {
                "prompt_tokens": 3,
                "completion_tokens": 5,
                "total_tokens": 8,
                "reasoning_tokens": 2,
            },
        )


if __name__ == "__main__":
    unittest.main()
