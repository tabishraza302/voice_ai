import pytest

from scripts import bench_agent_profiles
from scripts.bench_agent_profiles import local_model_is_installed, profile_cases


def test_profile_cases_use_compiled_preset_prompt_and_both_languages():
    cases = profile_cases()

    assert len(cases) == 6
    assert {case["slug"] for case in cases} == {
        "bilingual-assistant",
        "tech-tutor",
        "creative-storyteller",
    }
    assert {case["language"] for case in cases} == {"en", "hi"}
    assert all(case["system"] and case["model"] == "qwen3.5:4b" for case in cases)
    assert all(
        case["max_tokens"] > 0
        and case["reasoning_effort"] == "none"
        and case["keep_alive"]
        for case in cases
    )
    assert all("<system_policy" in case["system"] for case in cases)


def test_profile_bench_refuses_nonlocal_llm_endpoints():
    with pytest.raises(RuntimeError, match="only supports a local Ollama"):
        local_model_is_installed("https://example.test/v1", "qwen3.5:4b")


def test_profile_bench_rejects_redirects_without_echoing_url_credentials(monkeypatch):
    class RedirectingService:
        def open(self, request, timeout):
            raise bench_agent_profiles.urllib.error.HTTPError(
                request.full_url, 302, "redirect", {}, None
            )

    monkeypatch.setattr(bench_agent_profiles, "_local_opener", lambda: RedirectingService())
    with pytest.raises(RuntimeError) as error:
        local_model_is_installed(
            "http://user:secret@localhost:11434/v1", "qwen3.5:4b"
        )

    assert "secret" not in str(error.value)
    assert "redirected" in str(error.value)
