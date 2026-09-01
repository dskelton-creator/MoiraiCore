"""Tests for Gemini truncation detection & continuation (Feature 9)."""
import sys
import os
import json
import tempfile
from pathlib import Path

# Add scripts dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gemini_worker import (
    TruncatedResponseError,
    GeminiConfig,
    _validate_code_syntax,
    _auto_complete_text,
    _complete_json_continuation,
    _call_gemini,
    _call_openai_compatible,
    _call_gemini_native,
)


def test_truncated_response_error():
    """TruncatedResponseError stores partial, finish_reason, and config."""
    config = GeminiConfig(api_key="test", model="gemini-2.5-flash")
    try:
        raise TruncatedResponseError(
            partial="def foo():\n    pass\n",
            finish_reason="MAX_TOKENS",
            config=config,
        )
    except TruncatedResponseError as e:
        assert e.partial == "def foo():\n    pass\n"
        assert e.finish_reason == "MAX_TOKENS"
        assert e.config is config
        assert "MAX_TOKENS" in str(e)
        assert "20 chars" in str(e)  # len of the partial


def test_validate_python_syntax_ok():
    """Valid Python code passes ast.parse."""
    valid, msg = _validate_code_syntax(
        "def hello():\n    return 'world'\n",
        file_path="main.py",
    )
    assert valid is True, f"Expected valid, got: {msg}"
    assert msg == ""


def test_validate_python_syntax_bad():
    """Invalid Python code is caught by ast.parse."""
    code = "if True print('hello')"
    valid, msg = _validate_code_syntax(code, file_path="main.py")
    assert valid is False, f"Expected invalid, got valid. msg={msg}"
    assert "SyntaxError" in msg or "syntax error" in msg.lower()


def test_validate_python_syntax_missing_indent():
    """Missing indentation after function definition."""
    code = "def hello():\nreturn 'world'"
    valid, msg = _validate_code_syntax(code, file_path="main.py")
    assert valid is False, f"Expected invalid, got valid. msg={msg}"
    assert "SyntaxError" in msg or "syntax error" in msg.lower()


def test_validate_python_syntax_class():
    """Valid Python class definition."""
    code = """
class MyClass:
    def __init__(self, name: str):
        self.name = name

    def greet(self) -> str:
        return f"Hello, {self.name}"
"""
    valid, msg = _validate_code_syntax(code, file_path="models.py")
    assert valid is True, f"Expected valid, got: {msg}"


def test_validate_python_syntax_bad_class():
    """Invalid Python class with missing body."""
    code = "class MyClass:\n    pass\n\nclass AnotherClass:\n"
    valid, msg = _validate_code_syntax(code, file_path="models.py")
    assert valid is False, f"Expected invalid, got valid. msg={msg}"
    assert "SyntaxError" in msg or "syntax error" in msg.lower()


def test_validate_python_empty():
    """Empty code should be caught."""
    valid, msg = _validate_code_syntax("", file_path="main.py")
    assert valid is False


def test_validate_python_whitespace():
    """Whitespace-only code should be caught."""
    valid, msg = _validate_code_syntax("   \n  \n", file_path="main.py")
    assert valid is False


def test_validate_js_skip_no_node():
    """JS validation gracefully skips if node not found."""
    valid, msg = _validate_code_syntax(
        "const x = 1;",
        file_path="app.js",
    )
    # Should pass (skip) if node not installed, or validate if it is
    # Either way, it should not crash
    assert isinstance(valid, bool)


def test_validate_unknown_extension():
    """Unknown extensions should pass (no validation available)."""
    valid, msg = _validate_code_syntax("some text", file_path="data.csv")
    assert valid is True


def test_validate_no_extension():
    """No extension should pass (no validation available)."""
    valid, msg = _validate_code_syntax("some text", file_path="Makefile")
    assert valid is True


def test_auto_complete_base_case():
    """_auto_complete_text base case: depth >= max_depth returns partial."""
    config = GeminiConfig(api_key="test", model="gemini-2.5-flash")
    result = _auto_complete_text(
        "some partial text",
        config,
        "original prompt",
        file_path="test.py",
        depth=3,  # Already at max
        max_depth=3,
    )
    assert result == "some partial text"


def test_auto_complete_no_api_key():
    """_auto_complete_text with no API key returns partial."""
    config = GeminiConfig(api_key="", model="gemini-2.5-flash")
    # The continuation call will fail, returning partial
    result = _auto_complete_text(
        "partial\ncontent",
        config,
        "original prompt",
        file_path="test.py",
        depth=0,
        max_depth=3,
    )
    assert result == "partial\ncontent"


def test_complete_json_continuation_base_case():
    """_complete_json_continuation base case: depth >= max_depth returns partial."""
    config = GeminiConfig(api_key="test", model="gemini-2.5-flash")
    result = _complete_json_continuation(
        '{"name": "test",',
        config,
        "original prompt",
        depth=3,  # Already at max
        max_depth=3,
    )
    assert result == '{"name": "test",'


def test_complete_json_continuation_no_api_key():
    """_complete_json_continuation with no API key returns partial."""
    config = GeminiConfig(api_key="", model="gemini-2.5-flash")
    result = _complete_json_continuation(
        '{"name": "test",',
        config,
        "original prompt",
        depth=0,
        max_depth=3,
    )
    assert result == '{"name": "test",'


def test_validate_python_async_function():
    """Valid async Python code."""
    code = """
import asyncio

async def fetch_data(url: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.json()
"""
    valid, msg = _validate_code_syntax(code, file_path="utils.py")
    assert valid is True, f"Expected valid, got: {msg}"


def test_validate_python_try_except():
    """Valid try/except blocks."""
    code = """
try:
    result = risky_operation()
except ValueError as e:
    logger.error(f"Value error: {e}")
    return None
except Exception:
    logger.exception("Unexpected error")
    raise
finally:
    cleanup()
"""
    valid, msg = _validate_code_syntax(code, file_path="utils.py")
    assert valid is True, f"Expected valid, got: {msg}"


def test_validate_python_type_hints():
    """Valid Python with type hints."""
    code = """
from typing import Optional, List, Dict, Any

def process_items(
    items: List[Dict[str, Any]],
    filter_key: Optional[str] = None,
    max_count: int = 100,
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for item in items:
        if filter_key is None or filter_key in item:
            result.append(item)
            if len(result) >= max_count:
                break
    return result
"""
    valid, msg = _validate_code_syntax(code, file_path="utils.py")
    assert valid is True, f"Expected valid, got: {msg}"


def test_validate_html_should_pass():
    """HTML files should pass (no validation available)."""
    code = """<!DOCTYPE html>
<html>
<head><title>Test</title></head>
<body><h1>Hello</h1></body>
</html>"""
    valid, msg = _validate_code_syntax(code, file_path="index.html")
    assert valid is True, f"Expected valid HTML to pass, got: {msg}"


# ── Tier 2 swap: OpenAI-compatible transport tests (Feature 9 parity) ──
# NOTE: these avoid the pytest `monkeypatch` fixture so they also run under
# this file's plain `__main__` runner (which calls test_*() with no args).


def test_from_env_defaults_to_openai_compatible():
    """Default backend is OpenAI-compatible DeepSeek via OpenRouter."""
    import os as _os
    saved = {k: _os.environ.get(k) for k in ("TIER2_PROVIDER", "OPENROUTER_API_KEY", "GEMINI_API_KEY")}
    try:
        for k in saved:
            _os.environ.pop(k, None)
        cfg = GeminiConfig.from_env()
        assert cfg.provider == "openai"
        assert cfg.model == "deepseek/deepseek-v4-flash-0731"
        assert "openrouter" in cfg.base_url
    finally:
        for k, v in saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def test_from_env_gemini_when_requested():
    """TIER2_PROVIDER=gemini selects the native Gemini backend."""
    import os as _os
    saved = {k: _os.environ.get(k) for k in ("TIER2_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL")}
    try:
        _os.environ["TIER2_PROVIDER"] = "gemini"
        _os.environ["GEMINI_API_KEY"] = "gk"
        _os.environ["GEMINI_MODEL"] = "gemini-2.5-flash"
        cfg = GeminiConfig.from_env()
        assert cfg.provider == "gemini"
        assert cfg.model == "gemini-2.5-flash"
        assert "generativelanguage" in cfg.base_url
        assert "deepseek" not in cfg.model
    finally:
        for k, v in saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def test_dispatch_routes_to_openai():
    """_call_gemini routes to OpenAI-compatible transport by default."""
    import gemini_worker as gw
    called = {}
    orig_openai, orig_native = gw._call_openai_compatible, gw._call_gemini_native
    try:
        gw._call_openai_compatible = lambda *a, **k: called.setdefault("openai", True) or "ok"
        gw._call_gemini_native = lambda *a, **k: called.setdefault("native", True) or "native"
        cfg = GeminiConfig(api_key="test", provider="openai")
        _call_gemini(cfg, "hi")
        assert called.get("openai") is True
    finally:
        gw._call_openai_compatible = orig_openai
        gw._call_gemini_native = orig_native


def test_dispatch_routes_to_gemini_native():
    """_call_gemini routes to native transport when provider=gemini."""
    import gemini_worker as gw
    called = {}
    orig_native = gw._call_gemini_native
    try:
        gw._call_gemini_native = lambda *a, **k: called.setdefault("native", True) or "native"
        cfg = GeminiConfig(api_key="test", provider="gemini")
        _call_gemini(cfg, "hi")
        assert called.get("native") is True
    finally:
        gw._call_gemini_native = orig_native


def _mock_urlopen(response_json):
    """Return a fake response for urllib.request.urlopen."""
    class FakeResp:
        def __init__(self, body):
            self._body = body.encode("utf-8")
        def read(self):
            return self._body
        @property
        def status(self):
            return 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    return FakeResp(json.dumps(response_json))


def _with_fake_urlopen(response_json, fn):
    """Patch urllib.request.urlopen with a fake response for the call."""
    import urllib.request
    orig = urllib.request.urlopen
    try:
        urllib.request.urlopen = lambda req, timeout=10, **kw: _mock_urlopen(response_json)
        return fn()
    finally:
        urllib.request.urlopen = orig


def test_openai_truncation_maps_to_error():
    """finish_reason=='length' raises TruncatedResponseError (OpenAI signal)."""
    cfg = GeminiConfig(api_key="test", provider="openai")
    try:
        def call():
            return _call_openai_compatible(cfg, "gen code")
        _with_fake_urlopen({
            "choices": [{
                "message": {"content": "partial code"},
                "finish_reason": "length",
            }],
        }, call)
        assert False, "Expected TruncatedResponseError"
    except TruncatedResponseError as e:
        assert e.partial == "partial code"
        assert e.finish_reason == "length"


def test_openai_json_mode_parses():
    """OpenAI transport parses JSON content responses."""
    cfg = GeminiConfig(api_key="test", provider="openai")
    def call():
        return _call_openai_compatible(cfg, "gen json", json_mode=True)
    result = _with_fake_urlopen({
        "choices": [{
            "message": {"content": '{"name": "test", "ok": true}'},
            "finish_reason": "stop",
        }],
    }, call)
    assert isinstance(result, dict)
    assert result.get("name") == "test"


def test_openai_text_mode_returns_string():
    """OpenAI transport returns raw string for text (non-JSON) mode."""
    cfg = GeminiConfig(api_key="test", provider="openai")
    def call():
        return _call_openai_compatible(cfg, "gen code", json_mode=False)
    result = _with_fake_urlopen({
        "choices": [{
            "message": {"content": "def foo(): pass"},
            "finish_reason": "stop",
        }],
    }, call)
    assert result == "def foo(): pass"


def test_openai_error_response():
    """OpenAI transport returns error dict on API error."""
    cfg = GeminiConfig(api_key="bad", provider="openai")
    def call():
        return _call_openai_compatible(cfg, "hi", json_mode=False)
    result = _with_fake_urlopen({
        "error": {"message": "invalid api key"},
    }, call)
    assert isinstance(result, dict)
    assert "error" in result


def test_gemini_native_still_truncates():
    """Native transport still maps MAX_TOKENS to TruncatedResponseError."""
    cfg = GeminiConfig(api_key="test", provider="gemini")
    try:
        def call():
            return _call_gemini_native(cfg, "gen code")
        _with_fake_urlopen({
            "candidates": [{
                "content": {"parts": [{"text": "partial"}]},
                "finishReason": "MAX_TOKENS",
            }],
        }, call)
        assert False, "Expected TruncatedResponseError"
    except TruncatedResponseError as e:
        assert e.finish_reason == "MAX_TOKENS"


if __name__ == "__main__":
    # Run all tests
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            print(f"  ✓ {test_fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {test_fn.__name__}: ASSERTION: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {test_fn.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed, {len(tests)} total")
    sys.exit(0 if failed == 0 else 1)