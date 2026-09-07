"""Gemini Worker — Tier 2 Architect Engine (Gemini Pro).

Generates code architectures, directory structures, and Antigravity Artifacts
via the Google Gemini Pro API.
Key loaded from GEMINI_API_KEY environment variable — never hardcoded.

Includes automatic truncation detection & continuation (Feature 9):
- Gemini API responses with finishReason=MAX_TOKENS are detected
- Partial output is sent back in a follow-up request asking for completion
- Completed output is validated with ast.parse (Python) or node --check (JS/TS)
- All recursive with max_depth=3 to prevent infinite loops
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class TruncatedResponseError(Exception):
    """Raised when the Gemini API returns a truncated response (finishReason=MAX_TOKENS).

    The partial response is preserved so callers can complete it via continuation.
    """

    def __init__(self, partial: str, finish_reason: str, config: "GeminiConfig"):
        self.partial = partial
        self.finish_reason = finish_reason
        self.config = config
        super().__init__(
            f"Gemini response truncated: finishReason={finish_reason}, "
            f"got {len(partial)} chars"
        )


@dataclass
class GeminiConfig:
    """Configuration for Tier 2 worker.

    Provider-agnostic: supports any OpenAI-compatible chat-completions backend
    (default) and the Google Gemini native API as a fallback. The operator
    selects the provider and model per tier — no vendor is hardcoded.
    The class and function names are kept as `Gemini*` for backward
    compatibility with the 4 downstream consumers — the provider is just a
    transport detail inside the module.
    """
    api_key: str = ""  # Loaded from TIER2_API_KEY (or provider-specific vars)
    model: str = ""  # OpenAI-compatible model id — set via TIER2_MODEL or config
    provider: str = "openai"  # "openai" (OpenAI-compatible) or "gemini" (native)
    base_url: str = ""  # OpenAI-compatible endpoint — operator configures
    max_tokens: int = 8192
    temperature: float = 0.3  # Slightly creative for architecture
    timeout: int = 120  # seconds

    @classmethod
    def from_env(cls) -> "GeminiConfig":
        """Load config from environment variables.

        Default backend is OpenAI-compatible (any vendor); set TIER2_MODEL/TIER2_BASE_URL.
        Set TIER2_PROVIDER=gemini to use the Gemini native API instead.
        """
        provider = os.environ.get("TIER2_PROVIDER", "").strip().lower()
        if provider in ("", "openai", "openai-compatible", "custom"):
            # OpenAI-compatible default — operator picks the endpoint and model
            return cls(
                api_key=(
                    os.environ.get("TIER2_API_KEY")
                    or os.environ.get("OPENAI_API_KEY")
                    or ""
                ),
                model=os.environ.get("TIER2_MODEL", ""),
                provider="openai",
                base_url=os.environ.get(
                    "TIER2_BASE_URL", ""
                ),
                max_tokens=int(os.environ.get("TIER2_MAX_TOKENS", "8192")),
                temperature=float(os.environ.get("TIER2_TEMPERATURE", "0.3")),
                timeout=int(os.environ.get("TIER2_TIMEOUT", "120")),
            )
        # Gemini native fallback
        return cls(
            api_key=os.environ.get("GEMINI_API_KEY", ""),
            model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            provider="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            max_tokens=int(os.environ.get("TIER2_MAX_TOKENS", "8192")),
            temperature=float(os.environ.get("TIER2_TEMPERATURE", "0.3")),
            timeout=int(os.environ.get("TIER2_TIMEOUT", "120")),
        )


@dataclass
class Artifact:
    """An Antigravity Artifact — structured code blueprint."""
    artifact_type: str  # "directory_structure", "source_block", "dependency_list", "implementation_plan", "database_schema"
    name: str
    description: str
    content: dict  # Structured artifact data
    project_space: str
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "artifact_type": self.artifact_type,
            "name": self.name,
            "description": self.description,
            "content": self.content,
            "metadata": self.metadata,
        }

    def save(self, path: str) -> str:
        """Save artifact to project space."""
        artifact_path = Path(path) / "orchestration" / "artifacts"
        artifact_path.mkdir(parents=True, exist_ok=True)
        filename = f"{self.artifact_type}_{self.name.lower().replace(' ', '_')}.json"
        filepath = artifact_path / filename
        filepath.write_text(json.dumps(self.to_dict(), indent=2))
        return str(filepath)


@dataclass
class ArchitecturePlan:
    """Complete architecture plan from Tier 2."""
    project_name: str
    description: str
    tech_stack: dict
    directory_structure: dict
    database_schema: dict
    api_endpoints: list
    artifacts: list[Artifact]
    implementation_order: list[str]
    estimated_files: int

    def to_dict(self) -> dict:
        return {
            "project_name": self.project_name,
            "description": self.description,
            "tech_stack": self.tech_stack,
            "directory_structure": self.directory_structure,
            "database_schema": self.database_schema,
            "api_endpoints": self.api_endpoints,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "implementation_order": self.implementation_order,
            "estimated_files": self.estimated_files,
        }


def _call_gemini(config: GeminiConfig, prompt: str,
                 system_prompt: str = None,
                 json_mode: bool = True) -> dict | str:
    """Call the Tier 2 backend and return parsed response.

    Dispatches to the OpenAI-compatible transport (any vendor) or
    the Gemini native transport based on ``config.provider``.

    Raises TruncatedResponseError when output is truncated (finish_reason
    "length" / MAX_TOKENS). For JSON mode, the exception's .partial contains
    the raw text for recovery.
    """
    if config.provider == "gemini":
        return _call_gemini_native(config, prompt, system_prompt, json_mode)
    return _call_openai_compatible(config, prompt, system_prompt, json_mode)


def _call_openai_compatible(config: GeminiConfig, prompt: str,
                            system_prompt: str = None,
                            json_mode: bool = False) -> dict | str:
    """Call an OpenAI-compatible chat completions endpoint (any vendor).

    Maps OpenAI request/response shapes and translates the truncation signal
    (finish_reason == "length") into TruncatedResponseError so the existing
    Feature 9 continuation machinery keeps working.
    """
    import urllib.request

    url = f"{config.base_url.rstrip('/')}/chat/completions"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload: dict = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if "error" in result:
                return {"error": f"{result.get('error')}", "raw_response": str(result)}

            choices = result.get("choices", [])
            if not choices:
                return {"error": "no_choices", "raw_response": str(result)}

            choice = choices[0]
            # Reasoning models may leave content as None
            # if the token budget is exhausted by the reasoning pass — coerce
            # to "" so continuation logic never receives a None partial.
            content = (choice.get("message") or {}).get("content") or ""
            finish_reason = choice.get("finish_reason", "")

            # Truncation detection (OpenAI-compatible signal)
            if finish_reason == "length":
                raise TruncatedResponseError(
                    partial=content,
                    finish_reason=finish_reason,
                    config=config,
                )

            if json_mode:
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    # Try to extract JSON from markdown code block
                    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
                    if json_match:
                        return json.loads(json_match.group(1))
                    return {"raw_response": content}
            return content
    except TruncatedResponseError:
        raise  # Re-raise for callers to handle
    except Exception as e:
        return {"error": str(e)}


def _call_gemini_native(config: GeminiConfig, prompt: str,
                        system_prompt: str = None,
                        json_mode: bool = True) -> dict | str:
    """Call the Gemini native API (generativelanguage) and return parsed response.

    Raises TruncatedResponseError if finishReason is MAX_TOKENS.
    For JSON mode, the exception's .partial contains the raw text for recovery.
    """
    import urllib.request

    url = f"{config.base_url}/models/{config.model}:generateContent?key={config.api_key}"

    parts = []
    if system_prompt:
        parts.append({"text": f"## System Instructions\n{system_prompt}"})
    parts.append({"text": prompt})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": config.temperature,
            "maxOutputTokens": config.max_tokens,
        },
    }

    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            candidates = result.get("candidates", [])
            if not candidates:
                return {"error": "no_candidates", "raw_response": str(result)}

            candidate = candidates[0]
            content = candidate.get("content", {})
            text = content.get("parts", [{}])[0].get("text", "")
            finish_reason = candidate.get("finishReason", "")

            # Check for truncation - the core fix for Feature 9
            if finish_reason == "MAX_TOKENS":
                raise TruncatedResponseError(
                    partial=text,
                    finish_reason=finish_reason,
                    config=config,
                )

            if json_mode:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    # Try to extract JSON from markdown code block
                    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
                    if json_match:
                        return json.loads(json_match.group(1))
                    return {"raw_response": text}
            return text
    except TruncatedResponseError:
        raise  # Re-raise for callers to handle
    except Exception as e:
        return {"error": str(e)}


# ── Truncation Detection & Continuation (Feature 9) ──


def _validate_code_syntax(code: str, file_path: str = "") -> tuple[bool, str]:
    """Validate generated code syntax.

    Returns (is_valid, error_message).
    - Python (.py): uses ast.parse
    - JavaScript/TypeScript (.js/.ts/.jsx/.tsx): uses node --check
    - Other: returns True (no validation available)
    """
    if not code.strip():
        return False, "Code is empty"

    if file_path.endswith(".py"):
        try:
            ast.parse(code, filename=file_path or "<generated>")
            return True, ""
        except SyntaxError as e:
            return False, f"Python syntax error: {e}"

    if file_path.endswith((".js", ".mjs", ".cjs")):
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".js", mode="w", delete=False, encoding="utf-8"
            ) as f:
                f.write(code)
                tmp_path = f.name
            result = subprocess.run(
                ["node", "--check", tmp_path],
                capture_output=True, text=True, timeout=15,
            )
            Path(tmp_path).unlink(missing_ok=True)
            if result.returncode != 0:
                return False, f"JS syntax error: {result.stderr.strip()}"
            return True, ""
        except FileNotFoundError:
            # node not installed — skip JS validation
            return True, ""
        except subprocess.TimeoutExpired:
            return True, ""

    if file_path.endswith((".ts", ".tsx")):
        try:
            # Use deno check for TypeScript if available, otherwise skip
            result = subprocess.run(
                ["deno", "check", "-"],
                input=code, capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                return False, f"TS syntax error: {result.stderr.strip()}"
            return True, ""
        except FileNotFoundError:
            # deno not installed — skip TS validation
            return True, ""
        except subprocess.TimeoutExpired:
            return True, ""

    # No validation available for this file type
    return True, ""


def _auto_complete_text(partial_text: str, config: GeminiConfig,
                         original_prompt: str, file_path: str = "",
                         depth: int = 0, max_depth: int = 3) -> str:
    """Auto-complete a truncated Gemini response by sending a continuation request.

    Recursively appends continuation output until the response is complete
    or max_depth is reached. Each continuation request sends the partial output
    as context and asks Gemini to continue from where it left off.

    Args:
        partial_text: The partial output from a truncated Gemini response
        config: Gemini configuration
        original_prompt: The original prompt that was sent
        file_path: Target file path (for syntax validation)
        depth: Current continuation depth (internal)
        max_depth: Maximum number of continuation rounds

    Returns:
        The completed text (partial + all continuations concatenated)
    """
    if depth >= max_depth:
        return partial_text

    continuation_prompt = (
        f"## CONTINUATION REQUEST\n\n"
        f"You were generating output for the following task:\n\n"
        f"{original_prompt}\n\n"
        f"## YOUR OUTPUT SO FAR (truncated)\n\n"
        f"{partial_text}\n\n"
        f"## INSTRUCTION\n\n"
        f"Your previous response was cut off because it exceeded the maximum "
        f"output length. Continue EXACTLY from where you left off. "
        f"Do NOT repeat any of the content above. Do NOT add introductions "
        f"or conclusions. Just continue the output from the exact point "
        f"where it was truncated. Output the NEXT part only."
    )

    try:
        result = _call_gemini(config, continuation_prompt, json_mode=False)
        if isinstance(result, dict) and "error" in result:
            # Continuation failed, return what we have
            return partial_text

        continuation = str(result)
        combined = partial_text + continuation

        # If still truncated, recurse
        return _auto_complete_text(
            combined, config, original_prompt,
            file_path=file_path, depth=depth + 1, max_depth=max_depth,
        )

    except TruncatedResponseError as e:
        # Still truncated — append the new partial and recurse
        combined = partial_text + e.partial
        return _auto_complete_text(
            combined, config, original_prompt,
            file_path=file_path, depth=depth + 1, max_depth=max_depth,
        )

    except Exception:
        # Any other error, return what we have
        return partial_text


def _complete_json_continuation(partial_json_text: str, config: GeminiConfig,
                                 original_prompt: str,
                                 depth: int = 0, max_depth: int = 3) -> str:
    """Complete a truncated JSON response via continuation requests.

    Similar to _auto_complete_text but sends a JSON-specific continuation
    prompt that asks Gemini to complete the partial JSON object.
    """
    if depth >= max_depth:
        return partial_json_text

    continuation_prompt = (
        f"## CONTINUATION REQUEST\n\n"
        f"You were generating a JSON response for the following task:\n\n"
        f"{original_prompt}\n\n"
        f"## YOUR PARTIAL JSON OUTPUT (truncated)\n\n"
        f"{partial_json_text}\n\n"
        f"## INSTRUCTION\n\n"
        f"Your previous JSON response was truncated. Continue the JSON object "
        f"from the exact point where it was cut off. Return ONLY the remaining "
        f"JSON fields/values — do NOT repeat already-written content. "
        f"Do NOT wrap in markdown fences. Just continue the JSON."
    )

    try:
        result = _call_gemini(config, continuation_prompt, json_mode=True)
        if isinstance(result, dict) and "error" in result:
            return partial_json_text

        continuation = json.dumps(result) if isinstance(result, dict) else str(result)
        combined = partial_json_text + continuation

        # Try to parse as JSON
        try:
            json.loads(combined)
            return combined  # Valid JSON
        except json.JSONDecodeError:
            return _complete_json_continuation(
                combined, config, original_prompt,
                depth=depth + 1, max_depth=max_depth,
            )

    except TruncatedResponseError as e:
        combined = partial_json_text + e.partial
        return _complete_json_continuation(
            combined, config, original_prompt,
            depth=depth + 1, max_depth=max_depth,
        )

    except Exception:
        return partial_json_text


def generate_architecture(project_space: str, description: str,
                           requirements: list[str],
                           config: GeminiConfig = None) -> Optional[ArchitecturePlan]:
    """
    Generate a complete project architecture via the Tier 2 backend.

    Returns an ArchitecturePlan with directory structure, database schema,
    API endpoints, and Antigravity Artifacts.
    """
    if config is None:
        config = GeminiConfig.from_env()

    if not config.api_key:
        return None

    system_prompt = """You are a senior software architect. Generate complete project architectures 
as structured JSON. Always return valid JSON with these keys:
- project_name: string
- tech_stack: {language, framework, database, additional: []}
- directory_structure: nested dict representing folders and files
- database_schema: {tables: [{name, columns: [{name, type, constraints}], indexes: []}]}
- api_endpoints: [{method, path, description, request_body, response}]
- implementation_order: [list of file paths in order they should be created]
- estimated_files: integer

Be specific with file paths, column types, and endpoint structures."""

    user_prompt = f"""Generate a complete architecture for the following project:

## Description
{description}

## Requirements
{chr(10).join(f"- {r}" for r in requirements)}

## Project Space
{project_space}

Return ONLY valid JSON — no markdown, no commentary."""

    start_time = time.time()
    try:
        result = _call_gemini(config, user_prompt, system_prompt, json_mode=True)
    except TruncatedResponseError as e:
        # Auto-complete truncated JSON response (Feature 9)
        partial_text = e.partial
        completed_text = _complete_json_continuation(
            partial_text, config, user_prompt, depth=0, max_depth=3,
        )
        # Try to parse the completed JSON
        try:
            result = json.loads(completed_text)
        except json.JSONDecodeError:
            # Cannot parse JSON even after continuation — return None
            return None
    elapsed = int((time.time() - start_time) * 1000)

    if "error" in result:
        return None

    # Build artifacts from the plan
    artifacts = []
    if "directory_structure" in result:
        artifacts.append(Artifact(
            artifact_type="directory_structure",
            name="Project Layout",
            description="Complete directory and file structure",
            content={"structure": result["directory_structure"]},
            project_space=project_space,
        ))

    if "database_schema" in result:
        artifacts.append(Artifact(
            artifact_type="database_schema",
            name="Database Schema",
            description="Table definitions and relationships",
            content=result["database_schema"],
            project_space=project_space,
        ))

    if "api_endpoints" in result:
        artifacts.append(Artifact(
            artifact_type="implementation_plan",
            name="API Implementation Plan",
            description="Endpoint definitions and implementation order",
            content={
                "endpoints": result["api_endpoints"],
                "order": result.get("implementation_order", []),
            },
            project_space=project_space,
        ))

    return ArchitecturePlan(
        project_name=result.get("project_name", "Untitled"),
        description=result.get("description", description),
        tech_stack=result.get("tech_stack", {}),
        directory_structure=result.get("directory_structure", {}),
        database_schema=result.get("database_schema", {}),
        api_endpoints=result.get("api_endpoints", []),
        artifacts=artifacts,
        implementation_order=result.get("implementation_order", []),
        estimated_files=result.get("estimated_files", 0),
    )


def generate_code_block(project_space: str, spec: str,
                        file_path: str, config: GeminiConfig = None) -> str:
    """
    Generate a specific code block via the Tier 2 backend.

    Automatically detects and completes truncated responses (Feature 9):
    - If finishReason is MAX_TOKENS, sends a continuation request
    - Recursively completes until the response is whole or max_depth reached
    - Validates syntax with ast.parse (Python) or node --check (JS)

    Returns the generated source code as a string.
    """
    if config is None:
        config = GeminiConfig.from_env()

    if not config.api_key:
        return "ERROR: Tier 2 API key not set (TIER2_API_KEY / GEMINI_API_KEY)"

    system_prompt = """You are an expert software engineer. Generate clean, production-ready code.
|- Output ONLY the source code, no markdown fences, no comments explaining what you did
|- Follow best practices for the specified framework
|- Include proper error handling
|- Include type hints where appropriate"""

    user_prompt = f"""Generate code for: {spec}

Target file: {file_path}

Output the complete source code for this file."""

    try:
        result = _call_gemini(config, user_prompt, system_prompt, json_mode=False)
    except TruncatedResponseError as e:
        # Auto-complete the truncated response (Feature 9)
        result = _auto_complete_text(
            e.partial, config, user_prompt,
            file_path=file_path, depth=0, max_depth=3,
        )
        # Syntax-validate the completed code
        is_valid, error_msg = _validate_code_syntax(result, file_path)
        if not is_valid:
            result += f"\n\n# ⚠️ TRUNCATION WARNING: {error_msg}\n# Generated code may be incomplete — review before use."
        return result

    if isinstance(result, dict) and "error" in result:
        return f"ERROR: {result['error']}"

    code = str(result)

    # Syntax-validate completed code
    is_valid, error_msg = _validate_code_syntax(code, file_path)
    if not is_valid:
        code += f"\n\n# ⚠️ SYNTAX WARNING: {error_msg}\n# Review before use."

    return code


def is_gemini_available(config: GeminiConfig = None) -> bool:
    """Check if the Tier 2 backend is accessible and the key is valid.

    Dispatches by ``config.provider`` — sends a tiny chat completion to the
    OpenAI-compatible endpoint (any vendor) or a generateContent
    ping to the Gemini native API.
    """
    if config is None:
        config = GeminiConfig.from_env()

    if not config.api_key:
        return False

    import urllib.request
    try:
        if config.provider == "gemini":
            url = f"{config.base_url}/models/{config.model}:generateContent?key={config.api_key}"
            payload = json.dumps({
                "contents": [{"parts": [{"text": "ping"}]}],
                "generationConfig": {"maxOutputTokens": 10},
            }).encode()
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
        else:
            url = f"{config.base_url.rstrip('/')}/chat/completions"
            payload = json.dumps({
                "model": config.model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 10,
            }).encode()
            req = urllib.request.Request(
                url, data=payload, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {config.api_key}",
                },
            )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False
