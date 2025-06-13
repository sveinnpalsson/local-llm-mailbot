from .config     import LLAMA_SERVER_MODEL, LLAMA_SERVER_URL
import json
import logging
import time
import requests
from typing import List, Any, Dict
from smolagents import ChatMessage, ChatMessageStreamDelta

class LlamaServerModel:
    """
    smolagents-compatible model that proxies to llama-server
    and supports structured (JSON) and streaming calls.
    """

    def __init__(self, timeout: float = 120.0, **default_kwargs):
        self.default_kwargs = default_kwargs
        self.timeout = timeout

    def generate(self, messages: list[dict], **kwargs) -> ChatMessage:
        # SmolAgents may pass these—drop them
        kwargs.pop("response_format", None)
        kwargs.pop("tools_to_call_from", None)
        # Pop off stop_sequences if passed
        stop_seqs = kwargs.pop("stop_sequences", None)

        # Merge defaults + overrides + ensure parse_json=False
        call_kwargs = {
            **self.default_kwargs,
            **kwargs,
            "parse_json": False,
            "stop_sequences": stop_seqs,
        }
        text = llama_chat(messages, **call_kwargs) or ""
        msg = {"role": "assistant", "content": text}
        return ChatMessage.from_dict(msg)

    def generate_stream(
        self,
        messages: list[dict],
        stop_sequences: list[str] | None = None,
        **kwargs
    ):
        # Drop the same extras for streaming
        kwargs.pop("response_format", None)
        kwargs.pop("tools_to_call_from", None)

        # Build the payload for llama-server’s streaming endpoint
        payload = {
            "model": LLAMA_SERVER_MODEL,
            "messages": messages,
            **self.default_kwargs,
            **kwargs,
            "stream": True,
        }
        if stop_sequences:
            payload["stop"] = stop_sequences

        resp = requests.post(
            f"{LLAMA_SERVER_URL}/v1/chat/completions",
            json=payload,
            stream=True,
            timeout=self.timeout
        )
        resp.raise_for_status()

        buffer = ""
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            delta = event["choices"][0].get("delta", {})

            # text chunk
            if "content" in delta:
                chunk = delta["content"]
                buffer += chunk
                yield ChatMessageStreamDelta(content=chunk)

            # tool calls mid‐stream (if any)
            if delta.get("tool_calls"):
                yield ChatMessageStreamDelta(
                    content="",
                    tool_calls=[
                        tc for tc in delta["tool_calls"]
                    ]
                )

    __call__ = generate

def extract_json_objects(text: str) -> List[Dict]:
    """
    Scan `text` for all top-level JSON objects and return a list
    of dicts parsed from them. Silently skips any malformed JSON.
    """
    objs = []
    i = 0
    n = len(text)
    while True:
        # find the next opening brace
        start = text.find('{', i)
        if start == -1:
            break

        depth = 0
        for j in range(start, n):
            ch = text[j]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                # when we close the outermost brace, extract
                if depth == 0:
                    candidate = text[start:j+1]
                    try:
                        obj = json.loads(candidate)
                        objs.append(obj)
                    except json.JSONDecodeError:
                        # skip malformed JSON
                        pass
                    # move i past this object and continue scanning
                    i = j + 1
                    break
        else:
            # ran out of string without closing
            break

    return objs


def llama_chat(
    messages: list[dict],
    max_tokens: int = 256,
    temperature: float = 0.7,
    top_p: float = 0.9,
    presence_penalty: float = 1.2,
    retries: int = 4,
    timeout: float = 120.0,
    parse_json: bool = True,
    stop_sequences: list[str] | None = None,
) -> dict | None:
    """
    Sends a `messages` list to llama-server, retries on errors,
    extracts the final JSON object, parses it, and returns a dict.
    """
    for attempt in range(1, retries+1):
        message_len = sum([len(k['content']) for k in messages])
        payload = {
            "model":            LLAMA_SERVER_MODEL,
            "messages":         messages,
            "temperature":      temperature,
            "max_tokens":       max_tokens,
            "top_p":            top_p,
            "presence_penalty": presence_penalty
        }

        if stop_sequences:
            payload["stop"] = stop_sequences

        logging.debug("LLM payload (attempt %d): %s", attempt, payload)

        t0 = time.time()
        try:
            resp = requests.post(
                f"{LLAMA_SERVER_URL}/v1/chat/completions",
                json=payload,
                timeout=timeout
            )
            resp.raise_for_status()
        except KeyboardInterrupt:
            logging.warning("Aborted by user during LLM call (attempt %d)", attempt)
            raise
        except requests.exceptions.RequestException as e:
            logging.error("LLM request error on attempt %d: %s", attempt, e)
            continue
        
        print(f"Llama task finished: Input length: {message_len} Time: {time.time() - t0:.2f} seconds")
        raw = resp.json()["choices"][0]["message"]["content"]
        logging.debug("LLM raw output (attempt %d): %s", attempt, raw)

        if not parse_json:
            return raw
        
        js = extract_json_objects(raw)
        if not js:
            logging.warning("No JSON found on attempt %d, retrying...", attempt)
            continue

        return js

    logging.error("All %d LLM attempts failed.", retries)
    return None
