"""
Simplified LLM Provider using OpenAI-compatible format.

Supports all OpenAI-compatible API services (including proxy gateways
that front Gemini, Claude, etc.).

Usage:
    Set environment variables:
    - LLM_API_KEY: API key
    - LLM_BASE_URL: API endpoint (e.g. http://your-server:3000/v1)
    - LLM_MODEL: Model name (e.g. gemini-2.5-pro)

    Or pass directly:
    llm = SimpleLLMProvider(model="gemini-2.5-pro", api_key="xxx", base_url="xxx")
"""

from __future__ import annotations

import io
import os
import base64
import logging
import time
from typing import Any

import requests as http_requests
from PIL import Image
from openai import OpenAI

logger = logging.getLogger(__name__)

# ─── Constants ───

# Number of automatic retries on transient API failures
_MAX_RETRIES: int = 3
# Base delay (seconds) between retries — doubles on each attempt
_RETRY_BASE_DELAY: float = 1.0

SUPPORTED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


# ─── Image Utilities ───

def _fetch_image_data(url: str) -> tuple[bytes | None, str | None]:
    """Fetch image data and determine MIME type from a URL or base64 data URI."""
    if url.startswith("data:image"):
        try:
            header, encoded = url.split(",", 1)
            mime_type = header.split(":")[1].split(";")[0]
            data = base64.b64decode(encoded)
            return data, mime_type
        except Exception as e:
            logger.error("Error decoding base64 image URI: %s", e, exc_info=True)
            return None, None
    else:
        try:
            response = http_requests.get(url, stream=True, timeout=20)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type")
            mime_type = None
            if content_type:
                mime_type = content_type.split(";")[0].strip().lower()
            return response.content, mime_type
        except http_requests.exceptions.RequestException as e:
            logger.error("Error fetching image from URL '%s': %s", url, e, exc_info=True)
            return None, None
        except Exception as e:
            logger.error("Unknown error processing image from URL '%s': %s", url, e, exc_info=True)
            return None, None


def _ensure_image_base64_url(url: str) -> str | None:
    """Ensure an image is in base64 data URI format for the OpenAI API.

    * If it is already a data URI, returns as-is.
    * If it is an HTTP URL, fetches and converts.
    * Unsupported image formats are converted to PNG.
    """
    if url.startswith("data:image"):
        return url

    image_data, mime_type = _fetch_image_data(url)
    if not image_data:
        return None

    try:
        with Image.open(io.BytesIO(image_data)) as img:
            pil_format = img.format
            if pil_format:
                detected_mime = f"image/{pil_format.lower()}"
                mime_type = detected_mime

            if mime_type not in SUPPORTED_MIME_TYPES:
                logger.warning("Image format '%s' not supported — converting to PNG.", mime_type)
                img_byte_arr = io.BytesIO()
                if img.mode in ("RGBA", "LA", "P"):
                    img.save(img_byte_arr, format="PNG")
                else:
                    img.convert("RGB").save(img_byte_arr, format="PNG")
                image_data = img_byte_arr.getvalue()
                mime_type = "image/png"
    except Exception as e:
        logger.error("Error processing image: %s", e, exc_info=True)
        return None

    b64 = base64.b64encode(image_data).decode("utf-8")
    return f"data:{mime_type};base64,{b64}"


# ─── LLM Provider ───

class SimpleLLMProvider:
    """OpenAI-compatible LLM provider with automatic retries and tool calling.

    Works with native OpenAI, Azure, and any gateway that exposes the
    ``/v1/chat/completions`` endpoint.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = _MAX_RETRIES,
        request_timeout: float = 180.0,
        **kwargs: Any,
    ) -> None:
        self.model = model
        self.max_retries = max_retries
        self.logger = logger
        self.client = self._init_client(base_url, api_key, request_timeout)

    def _init_client(self, base_url: str | None, api_key: str | None, request_timeout: float) -> OpenAI:
        """Initialise the OpenAI-compatible client."""
        effective_base_url = base_url or os.getenv("LLM_BASE_URL")
        effective_api_key = api_key or os.getenv("LLM_API_KEY")

        # Backwards compat for legacy env vars
        if not effective_api_key:
            effective_api_key = os.getenv("GEMINI_API_KEY")
        if not effective_base_url:
            effective_base_url = os.getenv("GEMINI_BASE_URL")

        if not effective_api_key:
            raise ValueError(
                "API key not provided. Set LLM_API_KEY environment variable or pass api_key."
            )

        # Ensure /v1 suffix for OpenAI compatibility
        if effective_base_url:
            effective_base_url = effective_base_url.rstrip("/")
            if not effective_base_url.endswith("/v1"):
                effective_base_url += "/v1"

        self.logger.info("Initializing OpenAI-compatible client with base URL: %s", effective_base_url or "(default)")

        client = OpenAI(api_key=effective_api_key, base_url=effective_base_url, timeout=request_timeout)
        self.logger.info("OpenAI-compatible client initialized successfully.")
        return client

    # ─── Public API ───

    def list_models(self) -> list[str]:
        """List available models from the API."""
        try:
            models = self.client.models.list()
            return [m.id for m in models.data]
        except Exception as e:
            self.logger.error("Error listing models: %s", e, exc_info=True)
            return []

    def add_message(
        self, role: str, prompt: str, chat_history: list[dict[str, Any]], images: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Append a message to *chat_history* (OpenAI format)."""
        if not images:
            chat_history.append({"role": role, "content": prompt})
        else:
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for base64_image in images:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                })
            chat_history.append({"role": role, "content": content})
        return chat_history

    def invoke(
        self,
        chat_messages: list[dict[str, Any]],
        temperature: float = 0.5,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> str | None:
        """Call the LLM and return a text reply (with automatic retries)."""
        if not chat_messages:
            self.logger.error("No messages provided for API call.")
            return None

        self.logger.info("Invoking model '%s' with %d messages.", self.model, len(chat_messages))
        effective_max_tokens = max_tokens if max_tokens is not None else 8192
        processed_messages = self._process_messages(chat_messages)

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=processed_messages,
                    temperature=temperature,
                    max_tokens=effective_max_tokens,
                )

                if response.choices and response.choices[0].message.content:
                    content = response.choices[0].message.content
                    self.logger.info("Received response from model '%s'.", self.model)
                    return content.strip()

                finish_reason = response.choices[0].finish_reason if response.choices else "UNKNOWN"
                self.logger.warning("Response was empty or incomplete. Finish Reason: %s", finish_reason)
                return None

            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    self.logger.warning(
                        "API call attempt %d/%d failed (%s). Retrying in %.1fs…",
                        attempt, self.max_retries, e, delay,
                    )
                    time.sleep(delay)
                else:
                    self.logger.error("API call failed after %d attempts: %s", self.max_retries, e, exc_info=True)

        raise RuntimeError(f"API call failed after {self.max_retries} attempts: {last_error}") from last_error

    def invoke_with_tools(
        self,
        chat_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.5,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> Any:
        """Call the LLM with function-calling tools (with automatic retries).

        Returns the raw OpenAI ChatCompletion response object.
        """
        if not chat_messages:
            self.logger.error("No messages provided for API call.")
            return None

        self.logger.info(
            "Invoking model '%s' with %d messages and %d tools.",
            self.model, len(chat_messages), len(tools or []),
        )
        effective_max_tokens = max_tokens if max_tokens is not None else 8192
        processed_messages = self._process_messages(chat_messages)

        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": processed_messages,
            "temperature": temperature,
            "max_tokens": effective_max_tokens,
        }
        if tools:
            create_kwargs["tools"] = tools
            create_kwargs["tool_choice"] = "auto"

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(**create_kwargs)
                self.logger.info(
                    "Received response from model '%s' (finish_reason: %s).",
                    self.model,
                    response.choices[0].finish_reason if response.choices else "N/A",
                )
                return response
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    self.logger.warning(
                        "API tool call attempt %d/%d failed (%s). Retrying in %.1fs…",
                        attempt, self.max_retries, e, delay,
                    )
                    time.sleep(delay)
                else:
                    self.logger.error(
                        "API tool call failed after %d attempts: %s", self.max_retries, e, exc_info=True
                    )

        raise RuntimeError(f"API call failed after {self.max_retries} attempts: {last_error}") from last_error

    # ─── Internal ───

    def _process_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Process messages, converting image URLs to base64 if needed."""
        processed: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                new_content: list[dict[str, Any]] = []
                for item in content:
                    if item.get("type") == "image_url":
                        image_url_data = item.get("image_url", {})
                        url = image_url_data.get("url", "")
                        if url:
                            processed_url = _ensure_image_base64_url(url)
                            if processed_url:
                                new_content.append({
                                    "type": "image_url",
                                    "image_url": {"url": processed_url},
                                })
                            else:
                                self.logger.warning("Skipping unprocessable image URL: %s", url[:80])
                    else:
                        new_content.append(item)
                processed.append({"role": msg["role"], "content": new_content})
            else:
                processed.append(msg)
        return processed
