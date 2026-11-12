import json
import unittest

from provider import parse_exchange


def exchange(request, response):
    return parse_exchange(json.dumps(request).encode(), json.dumps(response).encode())


def stream(request, events):
    body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n'
    return parse_exchange(json.dumps(request).encode(), body.encode())


class ProviderTests(unittest.TestCase):
    def test_responses_json(self):
        result = exchange({'model': 'm', 'instructions': 'Be brief', 'input': 'Hello', 'temperature': 0.2}, {
            'id': 'r1', 'status': 'completed', 'output': [
                {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'Think'}]},
                {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Hi'}]},
                {'type': 'function_call', 'name': 'lookup', 'arguments': '{"q":"x"}'},
            ], 'usage': {'input_tokens': 3}})
        self.assertEqual([b['text'] for b in result['prompt']], ['Be brief', 'Hello'])
        self.assertEqual([b['kind'] for b in result['output']], ['reasoning', 'text', 'tool_call'])
        self.assertEqual(result['completion']['parameters']['temperature'], 0.2)
        self.assertEqual(result['usage']['input_tokens'], 3)

    def test_chat_choices_and_tools(self):
        result = exchange({'messages': [{'role': 'user', 'content': 'Hi'}, {'role': 'tool', 'tool_call_id': 'x', 'content': 'result'}]}, {
            'model': 'chat', 'choices': [{'index': 0, 'message': {'content': 'One'}, 'finish_reason': 'stop'},
                                         {'index': 1, 'message': {'content': None, 'tool_calls': [{'type': 'function', 'function': {'name': 'run', 'arguments': '{}'}}]}}]})
        self.assertEqual(result['prompt'][1]['kind'], 'tool_result')
        self.assertEqual(result['output'][0]['name'], 'Choice 1')
        self.assertEqual(result['output'][1]['name'], 'run')
        self.assertEqual(result['output'][1]['kind'], 'tool_call')

    def test_anthropic_json(self):
        result = exchange({'system': 'system', 'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'ask'}]}]},
                          {'model': 'claude', 'stop_reason': 'end_turn', 'content': [{'type': 'thinking', 'thinking': 'hmm'}, {'type': 'text', 'text': 'answer'}, {'type': 'tool_use', 'name': 'calc', 'input': {'n': 2}}], 'usage': {'output_tokens': 9}})
        self.assertEqual([b['kind'] for b in result['output']], ['reasoning', 'text', 'tool_call'])
        self.assertEqual(result['completion']['stop_reason'], 'end_turn')
        self.assertIn('"n": 2', result['output'][2]['text'])

    def test_gemini_json(self):
        result = exchange({'systemInstruction': {'parts': [{'text': 'system'}]}, 'contents': [{'role': 'user', 'parts': [{'text': 'ask'}]}], 'generationConfig': {'temperature': 0}},
                          {'modelVersion': 'gemini', 'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'answer'}, {'functionCall': {'name': 'f', 'args': {'x': 1}}}]}, 'finishReason': 'STOP'}], 'usageMetadata': {'totalTokenCount': 4}})
        self.assertEqual(result['prompt'][0]['text'], 'system')
        self.assertEqual(result['output'][1]['kind'], 'tool_call')
        self.assertEqual(result['usage']['totalTokenCount'], 4)

    def test_responses_stream_final_avoids_duplicates(self):
        result = stream({}, [{'type': 'response.reasoning_text.delta', 'delta': 'think'},
                             {'type': 'response.output_text.delta', 'delta': 'Hel'}, {'type': 'response.output_text.delta', 'delta': 'lo'},
                             {'type': 'response.completed', 'response': {'id': 'r', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Hello'}]}], 'usage': {'output_tokens': 2}}}])
        self.assertEqual([b['text'] for b in result['output']], ['Hello', 'think'])
        self.assertEqual(result['usage']['output_tokens'], 2)

    def test_chat_stream(self):
        result = stream({}, [{'choices': [{'index': 0, 'delta': {'content': 'Hel'}}, {'index': 1, 'delta': {'content': 'Other'}}]},
                             {'choices': [{'index': 0, 'delta': {'content': 'lo', 'tool_calls': [{'index': 0, 'function': {'name': 'f', 'arguments': '{"x":'}}]}}]},
                             {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '1}'}}]}, 'finish_reason': 'tool_calls'}], 'usage': {'total_tokens': 5}}])
        self.assertEqual([b['text'] for b in result['output']], ['Hello', 'Other', '{"x":1}'])
        self.assertEqual(result['output'][2]['name'], 'f')

    def test_anthropic_stream(self):
        result = stream({}, [{'type': 'message_start', 'message': {'id': 'm', 'model': 'claude', 'usage': {'input_tokens': 4}}},
                             {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'name': 'f', 'input': {}}},
                             {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': '{"a":'}},
                             {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': '1}'}},
                             {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 3}}])
        self.assertEqual(result['output'][0]['text'], '{"a":1}')
        self.assertEqual(result['usage'], {'input_tokens': 4, 'output_tokens': 3})

    def test_gemini_stream_and_error(self):
        result = stream({}, [{'candidates': [{'content': {'parts': [{'text': 'Hel'}]}}]}, {'candidates': [{'content': {'parts': [{'text': 'lo'}]}, 'finishReason': 'STOP'}], 'usageMetadata': {'totalTokenCount': 2}}])
        self.assertEqual(result['output'][0]['text'], 'Hello')
        err = exchange({}, {'error': {'message': 'Rate limited', 'code': 429}})
        self.assertIn('Rate limited', err['output'][0]['text'])

    def test_refusal_invalid_utf8_and_unknown(self):
        result = parse_exchange(b'{"input":"\xff"}', b'{"output":[{"type":"refusal","refusal":"No"},{"type":"custom","detail":"Useful"}]}')
        self.assertEqual([b['kind'] for b in result['output']], ['refusal', 'unknown'])
        self.assertIn('Useful', result['output'][1]['text'])
        self.assertTrue(result['notices'])


if __name__ == '__main__':
    unittest.main()
