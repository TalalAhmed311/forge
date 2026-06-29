"""DeepSeek adapter — OpenAI-compatible wire format on a different base URL."""

from __future__ import annotations

from typing import Optional

from forge.providers.openai import OpenAIProvider


class DeepSeekProvider(OpenAIProvider):
    name = "deepseek"

    def __init__(
        self,
        model: str = "deepseek-chat",
        context_window: int = 65536,
        base_url: str = "https://api.deepseek.com/v1",
        api_key: Optional[str] = None,
        api_key_env: str = "DEEPSEEK_API_KEY",
        max_retries: int = 4,
    ) -> None:
        super().__init__(
            model=model,
            context_window=context_window,
            base_url=base_url,
            api_key=api_key,
            api_key_env=api_key_env,
            max_retries=max_retries,
        )
