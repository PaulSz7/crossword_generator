"""Lightweight HTTP client for Gemini API interactions."""

from __future__ import annotations

import base64
import binascii
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import requests

from ..utils.logger import get_logger

if TYPE_CHECKING:
    from .prompt_log import PromptLog

LOGGER = get_logger(__name__)


class GeminiAPIError(RuntimeError):
    """Raised when the Gemini API responds with an error payload."""


class GeminiClient:
    """Minimal client around the public Gemini REST API."""

    API_BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        model_name: str = "gemini-2.5-flash",
        api_key_env: str = "GEMINI_API_KEY",
        model_env: str = "GEMINI_MODEL",
        timeout_seconds: float = 120.0,
        prompt_log: Optional["PromptLog"] = None,
    ) -> None:
        self.model_name = os.environ.get(model_env, model_name)
        self.api_key_env = api_key_env
        self.model_env = model_env
        self.timeout_seconds = timeout_seconds
        self._prompt_log = prompt_log
        self._api_key = os.environ.get(api_key_env)
        if not self._api_key:
            raise RuntimeError(
                f"Missing Gemini API key in environment variable {self.api_key_env}"
            )

    def generate_text(
        self,
        prompt: str,
        *,
        system_instruction: Optional[str] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        response_mime_type: Optional[str] = None,
        request_type: str = "unknown",
    ) -> str:
        """Send the prompt to Gemini and return the first candidate text."""
        url = f"{self.API_BASE}/models/{self.model_name}:generateContent"
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        payload: Dict[str, Any] = {"contents": contents}
        if system_instruction:
            payload["systemInstruction"] = {
                "role": "system",
                "parts": [{"text": system_instruction}],
            }
        generation_config: Dict[str, Any] = {}
        if response_schema:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = response_schema
        elif response_mime_type:
            generation_config["responseMimeType"] = response_mime_type
        if generation_config:
            payload["generationConfig"] = generation_config

        try:
            response = requests.post(
                url,
                params={"key": self._api_key},
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:  # pragma: no cover - network failure
            raise GeminiAPIError(f"Gemini request failed: {exc}") from exc

        data = response.json()
        text = self._extract_text(data)
        if not text:
            LOGGER.warning("Gemini response missing candidates: %s", data)
            raise GeminiAPIError("Gemini API response missing text candidates")
        if self._prompt_log is not None:
            self._prompt_log.record(request_type, prompt, text)
        return text

    def generate_text_grounded(
        self,
        prompt: str,
        *,
        system_instruction: Optional[str] = None,
        request_type: str = "unknown",
    ) -> str:
        """Send prompt to Gemini with Google Search grounding enabled (free-text response)."""
        url = f"{self.API_BASE}/models/{self.model_name}:generateContent"
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        payload: Dict[str, Any] = {
            "contents": contents,
            "tools": [{"google_search": {}}],
        }
        if system_instruction:
            payload["systemInstruction"] = {
                "role": "system",
                "parts": [{"text": system_instruction}],
            }

        try:
            response = requests.post(
                url,
                params={"key": self._api_key},
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:  # pragma: no cover - network failure
            raise GeminiAPIError(f"Gemini grounded request failed: {exc}") from exc

        data = response.json()
        text = self._extract_text(data)
        if not text:
            LOGGER.warning("Gemini grounded response missing candidates: %s", data)
            raise GeminiAPIError("Gemini API grounded response missing text candidates")
        if self._prompt_log is not None:
            self._prompt_log.record(request_type, prompt, text)
        return text

    @staticmethod
    def _extract_text(payload: Dict[str, Any]) -> Optional[str]:
        """Extract first textual candidate from the API payload."""
        candidates: List[Dict[str, Any]] = payload.get("candidates") or []
        for candidate in candidates:
            content = candidate.get("content") or {}
            parts: List[Dict[str, Any]] = content.get("parts") or []
            for part in parts:
                text = part.get("text")
                if text:
                    return text
                inline_data = part.get("inlineData")
                if inline_data:
                    decoded = GeminiClient._decode_inline_data(inline_data)
                    if decoded:
                        return decoded
        return None

    @staticmethod
    def _decode_inline_data(inline_data: Dict[str, Any]) -> Optional[str]:
        """Decode inline binary data (used for JSON structured responses)."""
        data = inline_data.get("data")
        if not data:
            return None
        try:
            decoded = base64.b64decode(data)
        except (ValueError, binascii.Error):  # pragma: no cover - data corruption
            return None
        try:
            return decoded.decode("utf-8")
        except UnicodeDecodeError:  # pragma: no cover - encoding issues
            return None
