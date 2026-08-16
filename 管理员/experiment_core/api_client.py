from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .config import ProviderConfig


class ModelAPIError(RuntimeError):
    pass


class OpenAICompatibleClient:
    """Small dependency-free client for OpenAI-compatible chat endpoints."""

    def __init__(self, config: ProviderConfig):
        self.config = config

    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip('/')
        if base.endswith('/chat/completions'):
            return base
        return base + '/chat/completions'

    def chat(
        self,
        messages: List[Dict[str, str]],
        max_tokens_override: Optional[int] = None,
        stream_override: Optional[bool] = None,
        allow_reasoning_fallback: bool = False,
    ) -> str:
        effective_stream = (
            stream_override
            if stream_override is not None
            else self.config.stream
        )
        payload: Dict[str, Any] = {
            'model': self.config.model,
            'messages': messages,
            'stream': effective_stream,
        }
        if self.config.temperature is not None:
            payload['temperature'] = self.config.temperature
        effective_max_tokens = (
            max_tokens_override
            if max_tokens_override is not None
            else self.config.max_tokens
        )
        if effective_max_tokens is not None:
            payload['max_tokens'] = effective_max_tokens
        payload.update(self.config.extra_body)

        headers = {
            'Authorization': 'Bearer ' + self.config.api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        headers.update(self.config.extra_headers)
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')

        last_error: Optional[Exception] = None
        attempts = max(1, self.config.max_retries)
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(
                self._endpoint(), data=body, headers=headers, method='POST'
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.timeout_seconds
                ) as response:
                    if effective_stream:
                        return self._read_stream(
                            response,
                            allow_reasoning_fallback=allow_reasoning_fallback,
                        )
                    raw = response.read().decode('utf-8')
                data = json.loads(raw)
                choice = data['choices'][0]
                message = choice['message']
                finish_reason = choice.get('finish_reason') or 'unknown'
                if finish_reason == 'length':
                    content = message.get('content')
                    reasoning = message.get('reasoning_content')
                    raise ModelAPIError(
                        '%s response was truncated by max_tokens '
                        '(content_chars=%d, reasoning_chars=%d)'
                        % (
                            self.config.name,
                            len(content) if isinstance(content, str) else 0,
                            len(reasoning) if isinstance(reasoning, str) else 0,
                        )
                    )
                content = message.get('content')
                if isinstance(content, str) and content.strip():
                    return content
                if isinstance(content, list):
                    pieces = []
                    for item in content:
                        if isinstance(item, dict) and item.get('text'):
                            pieces.append(str(item['text']))
                    if pieces:
                        return '\n'.join(pieces)
                reasoning = message.get('reasoning_content')
                reasoning_length = len(reasoning) if isinstance(reasoning, str) else 0
                raise ModelAPIError(
                    '%s returned no final answer (finish_reason=%s, reasoning_chars=%d)'
                    % (self.config.name, finish_reason, reasoning_length)
                )
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode('utf-8', errors='replace')
                last_error = ModelAPIError(
                    '%s HTTP %s: %s' % (
                        self.config.name, exc.code, detail[:1000]
                    )
                )
                if exc.code not in (408, 409, 429, 500, 502, 503, 504):
                    break
            except ModelAPIError as exc:
                last_error = exc
                if 'truncated by max_tokens' in str(exc):
                    break
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
                last_error = ModelAPIError('%s API error: %s' % (self.config.name, exc))

            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 8))

        raise ModelAPIError(str(last_error or 'Unknown model API error'))

    def _read_stream(
        self,
        response: Any,
        allow_reasoning_fallback: bool = False,
    ) -> str:
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        finish_reason: Optional[str] = None
        for raw_line in response:
            line = raw_line.decode('utf-8', errors='replace').strip()
            if not line or not line.startswith('data:'):
                continue
            data_text = line[5:].strip()
            if data_text == '[DONE]':
                break
            try:
                event = json.loads(data_text)
                choices = event.get('choices') or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get('finish_reason'):
                    finish_reason = str(choice['finish_reason'])
                delta = choice.get('delta') or {}
                if delta.get('reasoning_content'):
                    reasoning_parts.append(str(delta['reasoning_content']))
                if delta.get('content'):
                    content_parts.append(str(delta['content']))
            except (ValueError, TypeError, KeyError):
                continue
        content = ''.join(content_parts).strip()
        reasoning = ''.join(reasoning_parts).strip()
        if finish_reason == 'length':
            raise ModelAPIError(
                'Streaming response was truncated by max_tokens '
                '(content_chars=%d, reasoning_chars=%d)'
                % (len(content), len(reasoning))
            )
        if content:
            return content
        reasoning_length = len(reasoning)
        if allow_reasoning_fallback and reasoning:
            return reasoning
        raise ModelAPIError(
            'Streaming response contained no final answer '
            '(reasoning_chars=%d)' % reasoning_length
        )


def extract_json_object(text: str) -> Dict[str, Any]:
    """Parse a JSON object, tolerating a surrounding Markdown fence or prose."""
    stripped = text.strip()
    if stripped.startswith('```'):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        stripped = '\n'.join(lines).strip()
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass

    start = stripped.find('{')
    if start < 0:
        raise ValueError('No JSON object found')
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                value = json.loads(stripped[start:index + 1])
                if not isinstance(value, dict):
                    raise ValueError('Parsed JSON is not an object')
                return value
    raise ValueError('Unclosed JSON object')
