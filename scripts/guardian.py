"""
Guardian — PII & Credential Guardrail for MoiraiCore.

Scans agent responses before they reach the user.
Detects: PII, credentials, API keys, tokens, custom patterns.
Actions: pass, block, or redact.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class ScanResult(Enum):
    PASS = "pass"
    BLOCK = "block"
    REDACT = "redact"


@dataclass
class Finding:
    category: str
    pattern: str
    matched_text: str
    position: tuple  # (start, end)
    severity: str  # "high", "medium", "low"
    action: ScanResult


@dataclass
class ScanReport:
    original_length: int
    result: ScanResult
    findings: list[Finding] = field(default_factory=list)
    redacted_text: str = ""
    scan_time_ms: float = 0

    @property
    def has_findings(self) -> bool:
        return len(self.findings) > 0

    @property
    def high_severity_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "high")

    def to_dict(self) -> dict:
        return {
            "result": self.result.value,
            "findings_count": len(self.findings),
            "high_severity": self.high_severity_count,
            "categories": list(set(f.category for f in self.findings)),
            "scan_time_ms": self.scan_time_ms,
        }


# ── Detection Patterns ──

# PII Patterns
PII_PATTERNS = [
    # Social Security Numbers
    (r"\b\d{3}-\d{2}-\d{4}\b", "SSN", "high"),
    # Credit Card Numbers (Visa, MC, Amex, Discover)
    (r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6011)[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", "CREDIT_CARD", "high"),
    # Phone Numbers (US format)
    (r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", "PHONE", "medium"),
    # Email Addresses
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "EMAIL", "medium"),
    # Dates of Birth (common formats)
    (r"\b(?:0[1-9]|1[0-2])[/-](?:0[1-9]|[12]\d|3[01])[/-](?:19|20)\d{2}\b", "DOB", "medium"),
    # Street Addresses
    (r"\b\d+\s+[A-Z][a-zA-Z\s]+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|Way|Place|Pl)\.?\b", "ADDRESS", "medium"),
    # Passport Numbers
    (r"\b[A-Z]{1,2}\d{6,9}\b", "PASSPORT", "high"),
    # Driver's License (generic)
    (r"\b[A-Z]\d{7,14}\b", "LICENSE", "medium"),
]

# Credential Patterns
CREDENTIAL_PATTERNS = [
    # AWS Access Keys
    (r"\b(AKIA|A3T|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}\b", "AWS_ACCESS_KEY", "high"),
    # AWS Secret Keys (40-char base64-ish)
    (r"(?i)aws[_\s]*(?:secret|key|password)[\s:=]+[A-Za-z0-9/+=]{40}", "AWS_SECRET", "high"),
    # Generic API Keys (common patterns)
    (r"(?i)(?:api[_-]?key|apikey|api[_-]?secret)[\s:=]+[A-Za-z0-9_\-]{20,}", "API_KEY", "high"),
    # Bearer Tokens
    (r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*\b", "BEARER_TOKEN", "high"),
    # JWT Tokens
    (r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\b", "JWT_TOKEN", "high"),
    # GitHub PATs
    (r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b", "GITHUB_TOKEN", "high"),
    # Slack Tokens
    (r"\bxox[bpoas]-[A-Za-z0-9\-]{10,}\b", "SLACK_TOKEN", "high"),
    # Salesforce Tokens
    (r"\b00D[A-Za-z0-9]{15,}\b", "SFDC_TOKEN", "high"),
    # Stripe Keys
    (r"\b(?:sk|pk)_(?:test|live)_[A-Za-z0-9]{24,}\b", "STRIPE_KEY", "high"),
    # Twilio Keys
    (r"\bSK[A-Za-z0-9]{32}\b", "TWILIO_KEY", "high"),
    # Google API Keys
    (r"\bAIza[0-9A-Za-z_-]{35}\b", "GOOGLE_API_KEY", "high"),
    # Generic Secrets in config format
    (r"(?i)(?:secret|password|token|credential)[\s:=]+[\"']?[A-Za-z0-9_\-]{16,}[\"']?", "GENERIC_SECRET", "medium"),
    # Private Keys
    (r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----", "PRIVATE_KEY", "high"),
    # Connection Strings with passwords
    (r"(?i)(?:mongodb|postgres|mysql|redis|amqp|jdbc)://[^\s]+", "CONNECTION_STRING", "high"),
]

# Custom patterns loaded from config
CUSTOM_PATTERNS: list[tuple[str, str, str]] = []


def load_custom_patterns(config_path: Path = None):
    """Load custom detection patterns from config file."""
    global CUSTOM_PATTERNS
    if config_path is None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "guardian.json"

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            patterns = config.get("custom_patterns", [])
            CUSTOM_PATTERNS = [
                (p["pattern"], p.get("category", "CUSTOM"), p.get("severity", "medium"))
                for p in patterns
            ]
        except Exception:
            pass


def _mask_match(text: str, start: int, end: int) -> str:
    """Mask a matched string, keeping first and last 2 chars for context."""
    matched = text[start:end]
    if len(matched) <= 4:
        return "*" * len(matched)
    return matched[:2] + "*" * (len(matched) - 4) + matched[-2:]


def scan_text(text: str, custom_blocklist: list[str] = None) -> ScanReport:
    """
    Scan text for PII and credentials.

    Returns a ScanReport with findings and recommended action.
    """
    import time
    start_time = time.time()

    findings: list[Finding] = []
    redacted = text

    # Scan built-in PII patterns
    for pattern, category, severity in PII_PATTERNS:
        for match in re.finditer(pattern, text):
            findings.append(Finding(
                category=category,
                pattern=pattern,
                matched_text=match.group(),
                position=(match.start(), match.end()),
                severity=severity,
                action=ScanResult.BLOCK if severity == "high" else ScanResult.REDACT,
            ))

    # Scan credential patterns
    for pattern, category, severity in CREDENTIAL_PATTERNS:
        for match in re.finditer(pattern, text):
            findings.append(Finding(
                category=category,
                pattern=pattern,
                matched_text=match.group(),
                position=(match.start(), match.end()),
                severity=severity,
                action=ScanResult.BLOCK,
            ))

    # Scan custom patterns
    for pattern, category, severity in CUSTOM_PATTERNS:
        try:
            for match in re.finditer(pattern, text):
                findings.append(Finding(
                    category=category,
                    pattern=pattern,
                    matched_text=match.group(),
                    position=(match.start(), match.end()),
                    severity=severity,
                    action=ScanResult.BLOCK if severity == "high" else ScanResult.REDACT,
                ))
        except Exception:
            pass

    # Scan custom blocklist (exact match)
    if custom_blocklist:
        for term in custom_blocklist:
            if term.lower() in text.lower():
                findings.append(Finding(
                    category="BLOCKLIST",
                    pattern=f"exact:{term}",
                    matched_text=term,
                    position=(0, 0),
                    severity="high",
                    action=ScanResult.BLOCK,
                ))

    # Determine action
    if not findings:
        result = ScanResult.PASS
    elif any(f.action == ScanResult.BLOCK for f in findings):
        result = ScanResult.BLOCK
    else:
        result = ScanResult.REDACT

    # Generate redacted text if needed
    if result == ScanResult.REDACT:
        # Sort by position descending to replace from end to start
        sorted_findings = sorted(
            [f for f in findings if f.action == ScanResult.REDACT],
            key=lambda f: f.position[0],
            reverse=True,
        )
        redacted = text
        for finding in sorted_findings:
            start, end = finding.position
            redacted = redacted[:start] + _mask_match(text, start, end) + redacted[end:]

    elapsed = int((time.time() - start_time) * 1000)

    return ScanReport(
        original_length=len(text),
        result=result,
        findings=findings,
        redacted_text=redacted,
        scan_time_ms=elapsed,
    )


def scan_response(response: str, agent: str = "unknown",
                   custom_blocklist: list[str] = None) -> tuple[str, ScanReport]:
    """
    Scan an agent response. Returns (safe_text, report).

    - PASS: original text returned
    - BLOCK: empty text returned (response blocked)
    - REDACT: PII replaced with masked version
    """
    report = scan_text(response, custom_blocklist)

    if report.result == ScanResult.PASS:
        return response, report
    elif report.result == ScanResult.BLOCK:
        return "[BLOCKED] Response contained sensitive content that was blocked by Guardian.", report
    else:  # REDACT
        return report.redacted_text, report
