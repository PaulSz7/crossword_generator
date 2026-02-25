"""Lightweight HTTP client for Gemini API interactions."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

from ..utils.logger import get_logger

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
        timeout_seconds: float = 60.0,
    ) -> None:
        self.model_name = os.environ.get(model_env, model_name)
        self.api_key_env = api_key_env
        self.model_env = model_env
        self.timeout_seconds = timeout_seconds
        self._api_key = os.environ.get(api_key_env)
        if not self._api_key:
            raise RuntimeError(
                f"Missing Gemini API key in environment variable {self.api_key_env}"
            )

    def generate_text(self, prompt: str) -> str:
        """Send the prompt to Gemini and return the first candidate text."""
        url = f"{self.API_BASE}/models/{self.model_name}:generateContent"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
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
        return None
