"""Minimal provider plugin example.

Package this module and register:

[project.entry-points."geiter.providers"]
example = "provider_plugin:factory"
"""

from geiter.providers import ProviderAnswer


class ExampleProvider:
    name = "example"

    def answer(self, prompt: str) -> ProviderAnswer:
        return ProviderAnswer(
            provider=self.name,
            answer=f"Example provider saw: {prompt}",
            citations=[],
        )


def factory(**_kwargs):
    return ExampleProvider()
