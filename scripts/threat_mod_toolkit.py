#!/usr/bin/env python3
"""
MoiraiCore — Threat Modelling & Risk Assessment Toolkit
NIST SP 800-30 Rev.1 / SP 800-37 Rev.2 / SP 800-53 Rev.5 / CSF 2.0

Modules:
  1. stride_model       — STRIDE per asset/data flow (full threat enumeration)
  2. threat_catalogue   — NIST threat source taxonomy (adversarial, accidental, structural, environmental)
  3. risk_assess        — NIST 800-30 risk scoring (likelihood × impact → risk level)
  4. control_mapper     — Map threats → NIST SP 800-53 controls + CSF categories
  5. residual_risk      — Pre/post-mitigation risk comparison
  6. executive_report   — Full assessment report (markdown, board-ready)
  7. bowtie_analysis    — Bowtie diagram data (threats → consequences + barriers)

Usage:
    from threat_mod_toolkit import ThreatRiskEngine
    engine = ThreatRiskEngine()
    threats = engine.stride_model("User Login", "Web App")
    risk = engine.risk_assess("SQL Injection", "Web App", ...)
    report = engine.executive_report("KinlyPro SaaS", ...)
"""

from __future__ import annotations
import json
import math
import re
import time
from collections import defaultdict
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Optional


# ══════════════════════════════════════════════════════════════
# NIST REFERENCE DATA — SP 800-30 / 800-37 / 800-53 / CSF 2.0
# ══════════════════════════════════════════════════════════════

class _LabelEnum(IntEnum):
    """Base enum with label lookup via module-level maps."""

    @property
    def label(self) -> str:
        return _LABELS.get(type(self), {}).get(self.value, "Unknown")


# Module-level label maps (safe from IntEnum member detection)
_LABELS: dict[type, dict[int, str]] = {}


class Likelihood(_LabelEnum):
    """NIST SP 800-30 Rev.1 — Table E-2."""
    VERY_LOW = 1
    LOW = 2
    MODERATE = 3
    HIGH = 4
    VERY_HIGH = 5


_LABELS[Likelihood] = {
    1: "Very Low", 2: "Low", 3: "Moderate", 4: "High", 5: "Very High"
}


class Impact(_LabelEnum):
    """NIST SP 800-30 Rev.1 — Table E-3."""
    NEGLIGIBLE = 1
    MINOR = 2
    MODERATE = 3
    MAJOR = 4
    CATASTROPHIC = 5


_LABELS[Impact] = {
    1: "Negligible", 2: "Minor", 3: "Moderate", 4: "Major", 5: "Catastrophic"
}


class RiskLevel(_LabelEnum):
    """NIST SP 800-30 — Risk = Likelihood × Impact (1-25 mapped to levels)."""
    LOW = 1
    MODERATE = 2
    HIGH = 3
    CRITICAL = 4

    @staticmethod
    def from_score(score: int) -> "RiskLevel":
        if score <= 5:
            return RiskLevel.LOW
        if score <= 10:
            return RiskLevel.MODERATE
        if score <= 15:
            return RiskLevel.HIGH
        return RiskLevel.CRITICAL


_LABELS[RiskLevel] = {1: "Low", 2: "Moderate", 3: "High", 4: "Critical"}


# ── STRIDE Categories ────────────────────────────────────────
STRIDE = {
    "S": {
        "name": "Spoofing",
        "description": "Pretending to be something or someone else",
        "security_property": "Authentication",
        "examples": ["Session hijacking", "IP spoofing", "Phishing", "Credential theft", "JWT token forgery"],
        "attack_patterns": [
            "Brute-force authentication",
            "Session fixation",
            "Token replay attacks",
            "DNS spoofing / cache poisoning",
            "Email spoofing (phishing)",
        ],
    },
    "T": {
        "name": "Tampering",
        "description": "Modifying data, code, or configurations without authorisation",
        "security_property": "Integrity",
        "examples": ["Parameter manipulation", "File modification", "DB injection", "Config tampering", "Supply chain poisoning"],
        "attack_patterns": [
            "SQL / NoSQL injection",
            "Cross-site scripting (XSS)",
            "File path traversal",
            "API parameter tampering",
            "DLL / dependency hijacking",
        ],
    },
    "R": {
        "name": "Repudiation",
        "description": "Claiming to have not performed an action",
        "security_property": "Non-repudiation",
        "examples": ["Log deletion", "Missing audit trails", "Unsigned transactions", "Insufficient logging"],
        "attack_patterns": [
            "Log injection / deletion",
            "Clock manipulation",
            "MAC address spoofing",
            "Account sharing without trace",
        ],
    },
    "I": {
        "name": "Information Disclosure",
        "description": "Exposing information to unauthorised parties",
        "security_property": "Confidentiality",
        "examples": ["Data leaks", "Error message exposure", "Side-channel attacks", "Directory listing", "Over-permissioned APIs"],
        "attack_patterns": [
            "Verbose error messages revealing stack traces",
            "Insecure direct object references (IDOR)",
            "Unencrypted data at rest or in transit",
            "API response over-exposure",
            "Browser cache / history leakage",
        ],
    },
    "D": {
        "name": "Denial of Service",
        "description": "Denying or degrading service availability",
        "security_property": "Availability",
        "examples": ["Resource exhaustion", "Application-layer floods", "Infrastructure overload", "Logic bombs"],
        "attack_patterns": [
            "Layer 3/4 volumetric DDoS",
            "Layer 7 application-layer flood",
            "Algorithmic complexity attacks (ReDoS)",
            "Resource lockout (account lockout abuse)",
            "Database connection pool exhaustion",
        ],
    },
    "E": {
        "name": "Elevation of Privilege",
        "description": "Gaining capabilities without authorisation",
        "security_property": "Authorisation",
        "examples": ["Privilege escalation", "Broken access control", "Insecure deserialisation", "Path traversal"],
        "attack_patterns": [
            "Vertical privilege escalation",
            "Horizontal privilege escalation (BOLA)",
            "Insecure deserialisation",
            "Kernel/driver exploits",
            "Sudo / ACL misconfiguration",
        ],
    },
}

# ── NIST SP 800-30 Threat Sources ───────────────────────────
THREAT_SOURCES = {
    "adversarial": {
        "name": "Adversarial Actors",
        "subtypes": [
            {"key": "external_hacker", "name": "External Threat Actor (Opportunistic)", "typical_skill": "moderate", "typical_scope": "targeted"},
            {"key": "apt", "name": "Advanced Persistent Threat (Nation-State / Organised)", "typical_skill": "very_high", "typical_scope": "targeted"},
            {"key": "insider_malicious", "name": "Malicious Insider (Employee / Contractor)", "typical_skill": "high", "typical_scope": "targeted"},
            {"key": "insider_negligent", "name": "Negligent Insider (Accidental)", "typical_skill": "low", "typical_scope": "general"},
            {"key": "competitor", "name": "Competitor / Corporate Espionage", "typical_skill": "high", "typical_scope": "targeted"},
            {"key": "supply_chain", "name": "Supply Chain Compromise", "typical_skill": "high", "typical_scope": "targeted"},
            {"key": "hacktivist", "name": "Hacktivist / Ideologically Motivated", "typical_skill": "moderate", "typical_scope": "opportunistic"},
        ],
    },
    "accidental": {
        "name": "Accidental Events",
        "subtypes": [
            {"key": "user_error", "name": "User Error / Misconfiguration", "typical_skill": "low", "typical_scope": "general"},
            {"key": "admin_error", "name": "Admin Error / Malicious Maintenance", "typical_skill": "moderate", "typical_scope": "general"},
            {"key": "software_bug", "name": "Software Bug / Logic Error", "typical_skill": "moderate", "typical_scope": "general"},
        ],
    },
    "structural": {
        "name": "Structural Failures",
        "subtypes": [
            {"key": "hardware_failure", "name": "Hardware Failure", "typical_scope": "general"},
            {"key": "software_failure", "name": "Software Failure / Crash", "typical_scope": "general"},
            {"key": "capacity", "name": "Capacity Exhaustion (organic growth)", "typical_scope": "general"},
        ],
    },
    "environmental": {
        "name": "Environmental / Natural",
        "subtypes": [
            {"key": "natural_disaster", "name": "Natural Disaster (fire, flood, earthquake)", "typical_scope": "general"},
            {"key": "infrastructure", "name": "Infrastructure Failure (power, telecom, ISP)", "typical_scope": "general"},
            {"key": "pandemic", "name": "Pandemic / Workforce Disruption", "typical_scope": "general"},
        ],
    },
}

# ── NIST SP 800-53 Control Families (Rev.5) ─────────────────
CONTROLS = {
    "AC": {"name": "Access Control", "description": "Limit system access to authorised users"},
    "AU": {"name": "Audit and Accountability", "description": "Create, protect, and retain audit records"},
    "AT": {"name": "Awareness and Training", "description": "Ensure personnel are trained on security"},
    "CM": {"name": "Configuration Management", "description": "Establish and maintain configuration baselines"},
    "CP": {"name": "Contingency Planning", "description": "Establish incident response and recovery plans"},
    "IA": {"name": "Identification and Authentication", "description": "Identify and authenticate system users"},
    "IR": {"name": "Incident Response", "description": "Establish incident handling capability"},
    "MA": {"name": "Maintenance", "description": "Perform timely maintenance on systems"},
    "MP": {"name": "Media Protection", "description": "Protect system media and sanitise before disposal"},
    "PE": {"name": "Physical and Environmental Protection", "description": "Limit physical access to systems"},
    "PL": {"name": "Planning", "description": "Develop security and privacy plans"},
    "PM": {"name": "Program Management", "description": "Organisation-wide information security governance"},
    "PS": {"name": "Personnel Security", "description": "Screen individuals and define security roles"},
    "PT": {"name": "PII Processing and Transparency", "description": "Protect personally identifiable information"},
    "RA": {"name": "Risk Assessment", "description": "Periodically assess organisational risk"},
    "CA": {"name": "Security Assessment and Authorisation", "description": "Assess and authorise systems"},
    "SC": {"name": "System and Communications Protection", "description": "Protect system boundaries and communications"},
    "SI": {"name": "System and Information Integrity", "description": "Identify, report, and correct system flaws"},
    "SR": {"name": "Supply Chain Risk Management", "description": "Manage supply chain risks"},
}

# ── Threat → STRIDE → Control mapping ──────────────────────
STRIDE_CONTROLS: dict[str, list[str]] = {
    "S": ["IA", "AC", "SC"],      # Spoofing → AuthN, Access Control, Comm Protection
    "T": ["SC", "SI", "CM"],      # Tampering → Comm Protection, Integrity, Config Mgmt
    "R": ["AU", "SC", "IA"],      # Repudiation → Audit, Comm Protection, AuthN
    "I": ["AC", "SC", "MP", "PE"],# Info Disclosure → Access, Comms, Media, Physical
    "D": ["SC", "CP", "PE"],      # DoS → Comms, Contingency, Physical
    "E": ["AC", "IA", "CM"],      # Elevation → Access, AuthN, Config
}

# ── CSF 2.0 Categories ──────────────────────────────────────
CSF_FUNCTIONS = {
    "GV": "Govern", "ID": "Identify", "PR": "Protect",
    "DE": "Detect", "RS": "Respond", "RC": "Recover",
}

CSF_CATEGORIES = {
    "GV.OC": "Organisational Context", "GV.RM": "Risk Management Strategy",
    "GV.RR": "Roles and Responsibilities", "GV.PO": "Policy",
    "GV.SC": "Supply Chain Risk Management",
    "ID.AM": "Asset Management", "ID.RA": "Risk Assessment",
    "ID.IM": "Improvement",
    "PR.AC": "Identity and Access Management", "PR.DS": "Data Security",
    "PR.PS": "Platform Security", "PR.IR": "Technology Infrastructure Resilience",
    "DE.CM": "Continuous Monitoring", "DE.AE": "Adverse Event Analysis",
    "RS.AN": "Incident Analysis", "RS.MI": "Incident Mitigation",
    "RS.RP": "Incident Response Plan Execution",
    "RC.RP": "Incident Recovery Plan Execution", "RC.IM": "Improvements",
}

# ── Likelihood scoring rubric (NIST 800-30 Table E-2) ──────
LIKELIHOOD_RUBRIC = {
    1: {"label": "Very Low", "description": "Threat source is unlikely to initiate", "frequency": "Less than once per year"},
    2: {"label": "Low", "description": "Threat source has limited capability or intent", "frequency": "Once per year"},
    3: {"label": "Moderate", "description": "Threat source has moderate capability and intent", "frequency": "Once per quarter"},
    4: {"label": "High", "description": "Threat source is highly capable and motivated", "frequency": "Once per month"},
    5: {"label": "Very High", "description": "Threat source is actively targeting; exploits available", "frequency": "Weekly or more"},
}

# ── Impact scoring rubric (NIST 800-30 Table E-3) ───────────
IMPACT_RUBRIC = {
    1: {"label": "Negligible", "description": "No meaningful impact on operations or data"},
    2: {"label": "Minor", "description": "Limited degradation of services; minor data loss"},
    3: {"label": "Moderate", "description": "Significant degradation; partial data loss or disclosure"},
    4: {"label": "Major", "description": "Severe degradation; substantial data loss; regulatory breach"},
    5: {"label": "Catastrophic", "description": "Complete loss of service; massive data breach; organisation at risk"},
}


# ── Knowledge base for auto-assessment ─────────────────────
from threat_knowledge import (  # type: ignore[import-untyped]
    SYSTEM_PATTERNS, DOMAIN_OVERLAY, GENERIC_THREATS, CONTROL_BASELINES,
)


# ══════════════════════════════════════════════════════════════
# ThreatRiskEngine
# ══════════════════════════════════════════════════════════════

class ThreatRiskEngine:
    """NIST-based threat modelling and risk assessment engine."""

    def __init__(self):
        self._stride = STRIDE
        self._controls = CONTROLS
        self._threat_sources = THREAT_SOURCES
        self._csf_functions = CSF_FUNCTIONS
        self._csf_categories = CSF_CATEGORIES

    # ── Module 1: STRIDE Threat Model ─────────────────────────

    def stride_model(
        self,
        asset_name: str,
        asset_type: str = "Web Application",
        description: str = "",
        data_flows: Optional[list[dict]] = None,
        trust_boundaries: Optional[list[str]] = None,
        entry_points: Optional[list[str]] = None,
        assets: Optional[list[str]] = None,
    ) -> dict:
        """
        Generate a STRIDE threat model for an asset.

        Returns structured threat model with categories, threats per
        data flow, and trust boundary analysis.
        """
        data_flows = data_flows or []
        trust_boundaries = trust_boundaries or []
        entry_points = entry_points or []
        assets = assets or []
        now = datetime.now().isoformat()

        # Enumerate STRIDE threats for each data flow
        flow_threats = []
        for flow in data_flows:
            flow_name = flow.get("name", "unnamed")
            source = flow.get("source", "")
            destination = flow.get("destination", "")
            protocol = flow.get("protocol", "HTTPS")
            data_type = flow.get("data_type", "Structural")
            crosses_boundary = flow.get("crosses_boundary", False)

            threats = []
            for letter, info in self._stride.items():
                # Determine relevance score based on context
                relevance = _stride_relevance(letter, asset_type, protocol, data_type, crosses_boundary)

                if relevance >= 2:
                    for attack in info["attack_patterns"][:3]:
                        threats.append({
                            "id": f"THR-{letter}-{len(threats)+1:03d}",
                            "category": letter,
                            "category_name": info["name"],
                            "threat": f"{attack} via {flow_name}",
                            "description": f"{info['description']}: {attack}",
                            "relevance": relevance,
                            "data_flow": flow_name,
                            "security_property": info["security_property"],
                            "affected_assets": [source, destination],
                            "likelihood_hint": _likelihood_hint(letter, asset_type),
                        })

            flow_threats.append({
                "flow_name": flow_name,
                "source": source,
                "destination": destination,
                "protocol": protocol,
                "crosses_boundary": crosses_boundary,
                "threats": threats,
            })

        # Trust boundary threats
        boundary_threats = []
        for tb in trust_boundaries:
            for letter in ["S", "T", "I", "E"]:
                info = self._stride[letter]
                boundary_threats.append({
                    "id": f"TB-{len(boundary_threats)+1:03d}",
                    "category": letter,
                    "category_name": info["name"],
                    "threat": f"Unauthorised crossing at trust boundary: {tb}",
                    "boundary": tb,
                    "security_property": info["security_property"],
                })

        # Entry point threats
        entry_threats = []
        for ep in entry_points:
            entry_threats.append({
                "id": f"EP-{len(entry_threats)+1:03d}",
                "entry_point": ep,
                "threats": [
                    {"category": "S", "name": "Spoofing", "threat": f"Credential stuffing / brute force at {ep}"},
                    {"category": "T", "name": "Tampering", "threat": f"Parameter injection at {ep}"},
                    {"category": "I", "name": "Information Disclosure", "threat": f"Verbose error messages at {ep}"},
                    {"category": "D", "name": "Denial of Service", "threat": f"Rate-limit bypass flood at {ep}"},
                    {"category": "E", "name": "Elevation of Privilege", "threat": f"Broken access control at {ep}"},
                ],
            })

        all_threats = []
        for ft in flow_threats:
            all_threats.extend(ft["threats"])
        all_threats.extend(boundary_threats)
        for et in entry_threats:
            all_threats.extend([
                {**t, "entry_point": et["entry_point"]} for t in et["threats"]
            ])

        return {
            "asset_name": asset_name,
            "asset_type": asset_type,
            "description": description,
            "modelled_at": now,
            "summary": {
                "total_threats": len(all_threats),
                "by_category": _count_by(all_threats, "category"),
                "data_flows_analysed": len(data_flows),
                "trust_boundaries": len(trust_boundaries),
                "entry_points": len(entry_points),
            },
            "stride_reference": {k: v["name"] for k, v in self._stride.items()},
            "flow_threats": flow_threats,
            "boundary_threats": boundary_threats,
            "entry_threats": entry_threats,
            "all_threats": all_threats,
        }

    # ── Module 2: Risk Assessment (NIST 800-30) ──────────────

    def risk_assess(
        self,
        threats: list[dict],
        system_name: str = "",
        system_boundary: str = "",
        organisation: str = "",
    ) -> dict:
        """
        NIST SP 800-30 Rev.1 risk assessment.

        Each threat dict should have:
            - name: str
            - category: str (STRIDE letter)
            - likelihood: int (1-5, overrides rubric)
            - impact: int (1-5, overrides rubric)
            - threat_source: str (key from THREAT_SOURCES subtypes)
            - vulnerabilities: list[str]
            - existing_controls: list[str]
        """
        now = datetime.now().isoformat()
        risk_register = []
        total_risk_score = 0
        max_risk = 0

        for i, threat in enumerate(threats):
            name = threat.get("name", f"Threat-{i+1}")
            category = threat.get("category", "")
            vulnerabilities = threat.get("vulnerabilities", [])
            existing_controls = threat.get("existing_controls", [])

            # Auto-score if no explicit values
            likelihood = threat.get("likelihood")
            impact = threat.get("impact")

            if likelihood is None:
                likelihood = _auto_likelihood(category, threat.get("threat_source", ""), vulnerabilities)
            if impact is None:
                impact = _auto_impact(category, threat.get("impact_context", ""))

            likelihood = max(1, min(5, int(likelihood)))
            impact = max(1, min(5, int(impact)))

            risk_score = likelihood * impact
            risk_level = RiskLevel.from_score(risk_score).label
            max_risk = max(max_risk, risk_score)
            total_risk_score += risk_score

            # Map to NIST controls
            relevant_controls = _map_controls(category, vulnerabilities)

            risk_register.append({
                "risk_id": f"RSK-{i+1:04d}",
                "threat_name": name,
                "category": _stride_name(category) if category in self._stride else category,
                "threat_source": threat.get("threat_source", "Unknown"),
                "vulnerabilities": vulnerabilities,
                "existing_controls": existing_controls,
                "likelihood": {"value": likelihood, "label": Likelihood(likelihood).label},
                "impact": {"value": impact, "label": Impact(impact).label},
                "risk_score": risk_score,
                "risk_level": risk_level,
                "relevant_controls": relevant_controls,
                "csf_mapping": _csf_mapping(category),
            })

        avg_risk = total_risk_score / len(risk_register) if risk_register else 0
        by_level = _count_by(risk_register, "risk_level")

        return {
            "assessment_type": "NIST SP 800-30 Rev.1 Risk Assessment",
            "organisation": organisation,
            "system_name": system_name,
            "system_boundary": system_boundary,
            "assessed_at": now,
            "summary": {
                "total_risks": len(risk_register),
                "overall_risk_level": RiskLevel.from_score(max_risk).label if risk_register else "N/A",
                "max_risk_score": max_risk,
                "average_risk_score": round(avg_risk, 1),
                "risk_distribution": by_level,
                "critical_high_count": by_level.get("Critical", 0) + by_level.get("High", 0),
            },
            "rubric": {
                "likelihood": {str(k): v["label"] for k, v in LIKELIHOOD_RUBRIC.items()},
                "impact": {str(k): v["label"] for k, v in IMPACT_RUBRIC.items()},
                "risk_matrix": {
                    "Low": "Score 1-5", "Moderate": "Score 6-10",
                    "High": "Score 11-15", "Critical": "Score 16-25",
                },
            },
            "risk_register": sorted(risk_register, key=lambda r: r["risk_score"], reverse=True),
        }

    # ── Module 3: Control Mapper ──────────────────────────────

    def control_mapper(
        self,
        threats: list[str],
        risk_level: str = "Moderate",
        system_impact: str = "Moderate",
    ) -> dict:
        """
        Map identified threats → NIST SP 800-53 controls + CSF 2.0 categories.
        Returns prioritized control recommendations.
        """
        now = datetime.now().isoformat()
        control_set: dict[str, dict] = {}

        # Baselines by system impact level (FIPS 199 / SP 800-60)
        baselines = {
            "Low": ["AC-2", "AU-2", "IA-2", "SC-7", "SI-2"],
            "Moderate": ["AC-2", "AC-3", "AC-6", "AU-2", "AU-6", "IA-2", "IA-5",
                         "SC-7", "SC-8", "SI-2", "SI-3", "RA-5"],
            "High": ["AC-2", "AC-3", "AC-6", "AC-17", "AU-2", "AU-6", "AU-12",
                     "IA-2", "IA-5", "SC-7", "SC-8", "SC-12", "SI-2", "SI-3",
                     "IR-4", "CP-2", "CP-10", "RA-5", "CA-7"],
        }
        baseline_controls = baselines.get(system_impact, baselines["Moderate"])

        # Threat-specific controls
        for threat_desc in threats:
            threat_lower = threat_desc.lower()
            for letter, stride_info in self._stride.items():
                controls = STRIDE_CONTROLS.get(letter, [])
                for ctrl in controls:
                    if ctrl not in control_set:
                        control_set[ctrl] = {
                            "control_id": ctrl,
                            "name": self._controls[ctrl]["name"],
                            "description": self._controls[ctrl]["description"],
                            "addresses": [],
                            "priority": "medium",
                            "baseline": ctrl in baseline_controls,
                        }
                    if stride_info["name"].lower() in threat_lower or letter.lower() in threat_lower:
                        control_set[ctrl]["addresses"].append(threat_desc)

        # Prioritise based on risk level
        priority_map = {
            "Low": {"low": 1, "medium": 0, "high": 0},
            "Moderate": {"low": 0, "medium": 1, "high": 0},
            "High": {"low": 0, "medium": 0, "high": 1},
        }
        pri = priority_map.get(risk_level, priority_map["Moderate"])
        for ctrl in control_set.values():
            if len(ctrl["addresses"]) >= 3:
                ctrl["priority"] = "high" if pri["high"] else "medium"
            elif len(ctrl["addresses"]) >= 1:
                ctrl["priority"] = "medium" if pri["medium"] else "low"

        sorted_controls = sorted(
            control_set.values(),
            key=lambda c: ({"high": 0, "medium": 1, "low": 2}[c["priority"]], c["control_id"]),
        )

        return {
            "mapped_at": now,
            "system_impact": system_impact,
            "assessed_risk_level": risk_level,
            "threats_analysed": len(threats),
            "summary": {
                "total_controls": len(sorted_controls),
                "high_priority": sum(1 for c in sorted_controls if c["priority"] == "high"),
                "baseline_controls": sum(1 for c in sorted_controls if c["baseline"]),
                "above_baseline": sum(1 for c in sorted_controls if not c["baseline"]),
            },
            "control_baseline": baseline_controls,
            "controls": sorted_controls,
            "unaddressed_threats": [
                t for t in threats
                if not any(t in c["addresses"] for c in sorted_controls)
            ],
        }

    # ── Module 4: Residual Risk ──────────────────────────────

    def residual_risk(
        self,
        risk_register: list[dict],
        mitigations: list[dict],
    ) -> dict:
        """
        Calculate residual risk after mitigations.
        Each mitigation: {"risk_id": "RSK-0001", "description": "...",
                          "likelihood_reduction": 1, "impact_reduction": 1,
                          "status": "planned|implemented"}
        """
        now = datetime.now().isoformat()
        residual = []
        total_before = 0
        total_after = 0

        # Build mitigation lookup
        mit_map: dict[str, list[dict]] = defaultdict(list)
        for m in mitigations:
            mit_map[m.get("risk_id", "")].append(m)

        for risk in risk_register:
            rid = risk["risk_id"]
            orig_l = risk["likelihood"]["value"]
            orig_i = risk["impact"]["value"]
            orig_score = orig_l * orig_i

            total_before += orig_score

            mits = mit_map.get(rid, [])
            new_l = orig_l
            new_i = orig_i

            for m in mits:
                if m.get("status") in ("implemented", "effective"):
                    new_l = max(1, new_l - m.get("likelihood_reduction", 0))
                    new_i = max(1, new_i - m.get("impact_reduction", 0))
                elif m.get("status") == "planned":
                    # Planned = partial credit
                    new_l = max(1, new_l - m.get("likelihood_reduction", 0) // 2)
                    new_i = max(1, new_i - m.get("impact_reduction", 0) // 2)

            new_score = new_l * new_i
            total_after += new_score

            reduction = orig_score - new_score
            reduction_pct = round((reduction / orig_score) * 100, 1) if orig_score > 0 else 0

            residual.append({
                "risk_id": rid,
                "threat_name": risk["threat_name"],
                "original": {"score": orig_score, "level": RiskLevel.from_score(orig_score).label},
                "residual": {"score": new_score, "level": RiskLevel.from_score(new_score).label},
                "mitigations_applied": len(mits),
                "reduction": reduction,
                "reduction_pct": reduction_pct,
                "status": "retained" if new_score > 10 else "mitigated" if new_score > 5 else "accepted",
            })

        return {
            "assessed_at": now,
            "summary": {
                "total_risks": len(residual),
                "total_inherent_risk": total_before,
                "total_residual_risk": total_after,
                "overall_reduction_pct": round(((total_before - total_after) / total_before) * 100, 1) if total_before > 0 else 0,
                "mitigated_count": sum(1 for r in residual if r["status"] == "mitigated"),
                "retained_count": sum(1 for r in residual if r["status"] == "retained"),
                "accepted_count": sum(1 for r in residual if r["status"] == "accepted"),
            },
            "residual_risks": sorted(residual, key=lambda r: r["residual"]["score"], reverse=True),
        }

    # ── Module 5: Bowtie Analysis ─────────────────────────────

    def bowtie_analysis(
        self,
        threat_name: str,
        asset: str,
        consequences: Optional[list[str]] = None,
        preventive_barriers: Optional[list[str]] = None,
        recovery_measures: Optional[list[str]] = None,
        escalation_factors: Optional[list[str]] = None,
    ) -> dict:
        """
        Bowtie analysis data structure.

        Threat → Top Event → Consequences (right)
        Threat ← Barriers ← Escalation Factors (left)
        Recovery measures below the top event.
        """
        now = datetime.now().isoformat()
        consequences = consequences or []
        preventive_barriers = preventive_barriers or []
        recovery_measures = recovery_measures or []
        escalation_factors = escalation_factors or []

        return {
            "threat_name": threat_name,
            "asset": asset,
            "analysed_at": now,
            "top_event": f"Loss of {threat_name} controls on {asset}",
            "threats_left": [
                {"id": f"T-{i+1}", "description": t}
                for i, t in enumerate(escalation_factors or [threat_name])
            ],
            "preventive_barriers": [
                {"id": f"PB-{i+1}", "description": b, "effectiveness": "pending"}
                for i, b in enumerate(preventive_barriers)
            ],
            "recovery_measures": [
                {"id": f"RM-{i+1}", "description": m, "effectiveness": "pending"}
                for i, m in enumerate(recovery_measures)
            ],
            "consequences_right": [
                {"id": f"C-{i+1}", "description": c}
                for i, c in enumerate(consequences)
            ],
            "escalation_factors": escalation_factors,
        }

    # ── Module 6: Executive Report ────────────────────────────

    def executive_report(
        self,
        assessment: dict,
        organisation: str = "",
        scope: str = "",
        assessor: str = "MoiraiCore Threat Engine",
        classification: str = "Confidential",
    ) -> dict:
        """
        Generate a complete NIST-based threat & risk assessment report.
        Accepts the output of risk_assess() as input.
        """
        now = datetime.now().isoformat()
        summary = assessment.get("summary", {})
        register = assessment.get("risk_register", [])

        # Critical/High risks
        critical = [r for r in register if r["risk_level"] in ("Critical", "High")]
        moderate = [r for r in register if r["risk_level"] == "Moderate"]
        low = [r for r in register if r["risk_level"] == "Low"]

        # Top 10 risks
        top_risks = register[:10]

        # Control coverage
        all_controls = set()
        for r in register:
            for ctrl in r.get("relevant_controls", []):
                all_controls.add(ctrl["control_id"])

        # Risk heat-map data
        heatmap = _build_heatmap(register)

        # Generate recommendations
        recommendations = _generate_recommendations(critical, moderate, list(all_controls))

        # Build markdown report
        report_md = _build_markdown_report(
            organisation, scope, assessor, classification,
            summary, critical, moderate, low, top_risks,
            recommendations, heatmap, all_controls, now,
        )

        return {
            "report_type": "NIST Threat & Risk Assessment Report",
            "organisation": organisation,
            "scope": scope,
            "assessor": assessor,
            "classification": classification,
            "generated_at": now,
            "executive_summary": {
                "total_risks_assessed": summary.get("total_risks", 0),
                "overall_risk_level": summary.get("overall_risk_level", "N/A"),
                "critical_high_count": summary.get("critical_high_count", 0),
                "control_families_addressed": len(all_controls),
            },
            "risk_distribution": summary.get("risk_distribution", {}),
            "top_risks": [
                {
                    "id": r["risk_id"],
                    "name": r["threat_name"],
                    "score": r["risk_score"],
                    "level": r["risk_level"],
                    "controls": [c["control_id"] for c in r.get("relevant_controls", [])],
                }
                for r in top_risks
            ],
            "critical_risks": [
                {"id": r["risk_id"], "name": r["threat_name"], "score": r["risk_score"],
                 "likelihood": r["likelihood"]["label"], "impact": r["impact"]["label"]}
                for r in critical
            ],
            "recommendations": recommendations,
            "control_coverage": {
                "families_addressed": sorted(all_controls),
                "coverage_by_family": _controls_by_family(sorted(all_controls)),
            },
            "risk_heatmap": heatmap,
            "full_markdown_report": report_md,
        }

    # ── Module 7: Threat Catalogue Lookup ─────────────────────

    def threat_catalogue(
        self,
        category: Optional[str] = None,
        source_type: Optional[str] = None,
    ) -> dict:
        """Query the NIST threat source catalogue."""
        now = datetime.now().isoformat()

        if source_type:
            src = self._threat_sources.get(source_type, {})
            return {
                "queried_at": now,
                "source_type": source_type,
                "name": src.get("name", "Unknown"),
                "subtypes": src.get("subtypes", []),
            }

        if category:
            cat_threats = []
            for letter, info in self._stride.items():
                if any(category.lower() in e.lower() for e in info["examples"]):
                    cat_threats.append(info)
            return {
                "queried_at": now,
                "category": category,
                "matching_threats": cat_threats,
            }

        return {
            "queried_at": now,
            "stride_categories": {k: v["name"] for k, v in self._stride.items()},
            "threat_sources": {k: v["name"] for k, v in self._threat_sources.items()},
            "control_families": {k: v["name"] for k, v in self._controls.items()},
            "csf_functions": self._csf_functions,
        }

    # ── Module 8: RMF Step Tracker (NIST 800-37) ─────────────

    def rmf_tracker(
        self,
        system_name: str,
        current_step: str = "Categorize",
        steps: Optional[dict] = None,
    ) -> dict:
        """
        NIST SP 800-37 Rev.2 — Risk Management Framework step tracker.
        Steps: Categorize → Select → Implement → Assess → Authorize → Monitor
        """
        now = datetime.now().isoformat()
        rmf_steps = {
            "Categorize": {"order": 1, "description": "Categorise system per FIPS 199", "status": "not_started", "artifacts": []},
            "Select": {"order": 2, "description": "Select baseline controls per SP 800-53", "status": "not_started", "artifacts": []},
            "Implement": {"order": 3, "description": "Implement and document controls", "status": "not_started", "artifacts": []},
            "Assess": {"order": 4, "description": "Assess control effectiveness", "status": "not_started", "artifacts": []},
            "Authorize": {"order": 5, "description": "Authorise system operation", "status": "not_started", "artifacts": []},
            "Monitor": {"order": 6, "description": "Continuous monitoring", "status": "not_started", "artifacts": []},
        }

        if steps:
            for k, v in steps.items():
                if k in rmf_steps:
                    rmf_steps[k].update(v)

        # Mark current step and previous as complete
        step_order = list(rmf_steps.keys())
        current_idx = step_order.index(current_step) if current_step in step_order else 0
        for i, step_name in enumerate(step_order):
            if i < current_idx:
                rmf_steps[step_name]["status"] = "completed"
            elif i == current_idx:
                rmf_steps[step_name]["status"] = "in_progress"
            # future steps remain "not_started"

        completed = sum(1 for s in rmf_steps.values() if s["status"] == "completed")
        progress = round((completed / len(rmf_steps)) * 100)

        return {
            "system_name": system_name,
            "framework": "NIST SP 800-37 Rev.2",
            "updated_at": now,
            "current_step": current_step,
            "progress_pct": progress,
            "steps": rmf_steps,
        }

    # ── Module 9: Auto-Assess from Description ────────────────

    def auto_assess(
        self,
        description: str,
        system_name: str = "",
        assessor: str = "MoiraiCore Threat Engine",
    ) -> dict:
        """
        Generate a complete threat & risk assessment from a natural-language
        system description alone. No manual data flows or threat enumeration.

        Steps:
          1. Classify the system type (web app, API, SaaS, mobile, infra…)
          2. Detect the domain (healthcare, finance, ecommerce, SaaS, generic)
          3. Infer default data flows, trust boundaries, entry points
          4. Build threat list from generic + domain-specific templates
          5. Score each threat (likelihood × impact) with auto-rubric
          6. Map to NIST 800-53 controls + CSF 2.0
          7. Generate executive report with recommendations
          8. Package everything into a single response
        """
        now = datetime.now().isoformat()
        desc_lower = description.lower()
        name = system_name or description[:60].strip()

        # ── Step 1: Detect system type ────────────────────────
        sys_type = self._classify_system(desc_lower)
        arch = SYSTEM_PATTERNS.get(sys_type, SYSTEM_PATTERNS["web_app"])

        # ── Step 2: Detect domain overlay ──────────────────────
        domain_key, overlay = self._detect_domain(desc_lower)

        # ── Step 3: Infer architecture ─────────────────────────
        data_flows = [dict(f) for f in arch["default_data_flows"]]
        trust_boundaries = list(arch["default_trust_boundaries"])
        entry_points = list(arch["default_entry_points"])
        assets = list(arch["common_assets"])

        # Augment from domain overlay
        if overlay:
            assets.extend(overlay.get("extra_assets", []))
            # Add domain-specific entry points for healthcare
            if domain_key == "healthcare":
                entry_points.extend(["/patient/portal", "/clinician/login", "/records", "/booking"])
            elif domain_key == "finance":
                entry_points.extend(["/payment", "/transfer", "/account", "/kyc"])
            elif domain_key == "ecommerce":
                entry_points.extend(["/cart", "/checkout", "/account", "/orders"])
            elif domain_key == "saas":
                entry_points.extend(["/tenant/", "/billing", "/settings", "/api/docs"])

        # ── Step 4: Build STRIDE threat model ──────────────────
        stride_result = self.stride_model(
            asset_name=name,
            asset_type=arch.get("keywords", [""])[0].title() if arch.get("keywords") else "System",
            description=description,
            data_flows=data_flows,
            trust_boundaries=trust_boundaries,
            entry_points=entry_points,
            assets=assets,
        )
        stride_threats = stride_result.get("all_threats", [])

        # ── Step 5: Build risk assessment ──────────────────────
        # Start with generic threats for the detected system type
        threat_profile = arch.get("threat_profile", "public_web")
        generic = GENERIC_THREATS.get(threat_profile, GENERIC_THREATS["public_web"])

        # Build risk entry list
        risk_threats: list[dict] = []

        # Add generic threats (deduplicated by name)
        seen_names: set[str] = set()
        for t in generic:
            if t["name"] not in seen_names:
                risk_threats.append(dict(t))
                seen_names.add(t["name"])

        # Add domain-specific threats
        if overlay:
            for t in overlay.get("extra_threats", []):
                if t["name"] not in seen_names:
                    risk_threats.append(dict(t))
                    seen_names.add(t["name"])

        # Add high-relevance STRIDE threats (relevance >= 4)
        for st in stride_threats:
            if st.get("relevance", 0) >= 4:
                entry = {
                    "name": st.get("threat", ""),
                    "category": st.get("category", ""),
                    "threat_source": "external_hacker",
                    "likelihood": st.get("likelihood_hint", 3),
                    "impact": 3,
                    "vulnerabilities": [],
                }
                if entry["name"] and entry["name"] not in seen_names:
                    risk_threats.append(entry)
                    seen_names.add(entry["name"])

        # Add data-type specific risk boosters in description
        extra_threats = self._detect_description_threats(desc_lower)
        for et in extra_threats:
            if et["name"] not in seen_names:
                risk_threats.append(et)
                seen_names.add(et["name"])

        # Determine system impact from overlay or default
        system_impact = "Moderate"
        if overlay:
            system_impact = overlay.get("system_impact", "Moderate")
        elif "pii" in desc_lower or "phi" in desc_lower or "health" in desc_lower or "payment" in desc_lower:
            system_impact = "High"

        # Run risk assessment
        risk_result = self.risk_assess(
            threats=risk_threats,
            system_name=name,
            system_boundary=f"Inferred: {sys_type} ({domain_key or 'generic'})",
            organisation="",
        )

        # ── Step 6: Control mapping ───────────────────────────
        control_result = self.control_mapper(
            threats=[t["name"] for t in risk_threats],
            risk_level=risk_result["summary"].get("overall_risk_level", "Moderate"),
            system_impact=system_impact,
        )

        # ── Step 7: Executive report ──────────────────────────
        report_result = self.executive_report(
            assessment=risk_result,
            organisation="",
            scope=f"{name} — {description[:120]}",
            assessor=assessor,
        )

        # ── Step 8: High-level summary ────────────────────────
        risk_dist = risk_result["summary"].get("risk_distribution", {})
        critical_high = risk_result["summary"].get("critical_high_count", 0)

        summary = self._build_executive_summary(
            name, description, sys_type, domain_key,
            len(stride_threats), len(risk_threats),
            risk_dist, critical_high,
            risk_result["summary"].get("overall_risk_level", "Moderate"),
            control_result, risk_result,
        )

        return {
            "assessment_type": "NIST Auto-Assessment (from description)",
            "generated_at": now,
            "input_description": description,
            "inferred": {
                "system_type": sys_type,
                "domain": domain_key or "generic",
                "system_impact_level": system_impact,
                "data_flows_inferred": len(data_flows),
                "trust_boundaries": trust_boundaries,
                "entry_points": entry_points,
                "assets_identified": assets[:10],
            },
            "executive_summary": summary,
            "stride_model": stride_result["summary"],
            "risk_assessment": risk_result["summary"],
            "control_mapping": control_result["summary"],
            "risk_register": risk_result.get("risk_register", [])[:15],
            "top_recommendations": report_result.get("recommendations", [])[:10],
            "control_families": control_result["controls"][:12],
            "regulatory_frameworks": overlay.get("regulatory_frameworks", []) if overlay else [],
            "full_markdown_report": report_result.get("full_markdown_report", ""),
            "_full_stride": stride_result,
            "_full_risk": risk_result,
            "_full_controls": control_result,
        }

    def _classify_system(self, desc_lower: str) -> str:
        """Detect system type from description."""
        scores: dict[str, int] = {}
        for sys_type, info in SYSTEM_PATTERNS.items():
            score = sum(1 for kw in info["keywords"] if kw in desc_lower)
            if score > 0:
                scores[sys_type] = score
        if not scores:
            return "web_app"  # default
        return max(scores, key=scores.get)

    def _detect_domain(self, desc_lower: str) -> tuple[str, dict]:
        """Detect domain overlay from description."""
        for domain_key, overlay in DOMAIN_OVERLAY.items():
            if any(kw in desc_lower for kw in overlay.get("keywords", [])):
                return domain_key, overlay
        return "", {}

    def _detect_description_threats(self, desc_lower: str) -> list[dict]:
        """Detect additional threats mentioned in the description."""
        detected: list[dict] = []
        threat_indicators = [
            ("vulnerability", {"name": "Known Vulnerability — Unpatched", "category": "T", "source": "external_hacker", "l": 3, "i": 4}),
            ("breach", {"name": "Historical Breach Pattern", "category": "I", "source": "external_hacker", "l": 3, "i": 5}),
            ("hack", {"name": "Active Threat Landscape", "category": "T", "source": "external_hacker", "l": 4, "i": 4}),
            ("phishing", {"name": "Phishing / Social Engineering", "category": "S", "source": "external_hacker", "l": 4, "i": 3}),
            ("ransomware", {"name": "Ransomware Attack", "category": "D", "source": "external_hacker", "l": 3, "i": 5}),
            ("ddos", {"name": "Distributed Denial of Service", "category": "D", "source": "hacktivist", "l": 3, "i": 3}),
            ("insider", {"name": "Insider Threat", "category": "I", "source": "insider_malicious", "l": 3, "i": 4}),
            ("third-party", {"name": "Third-Party / Supply Chain Risk", "category": "T", "source": "supply_chain", "l": 3, "i": 4}),
            ("outsource", {"name": "Outsourced Service Risk", "category": "T", "source": "supply_chain", "l": 2, "i": 3}),
            ("legacy", {"name": "Legacy System Vulnerabilities", "category": "T", "source": "external_hacker", "l": 3, "i": 4}),
            ("cloud", {"name": "Cloud Misconfiguration", "category": "I", "source": "accidental", "l": 3, "i": 4}),
            ("aws", {"name": "AWS Cloud Security", "category": "I", "source": "accidental", "l": 3, "i": 4}),
            ("docker", {"name": "Container Security", "category": "E", "source": "external_hacker", "l": 3, "i": 4}),
            ("kubernetes", {"name": "K8s Cluster Security", "category": "E", "source": "external_hacker", "l": 2, "i": 4}),
            ("api-exposed", {"name": "Exposed API Endpoints", "category": "I", "source": "external_hacker", "l": 3, "i": 4}),
        ]
        for indicator, template in threat_indicators:
            if indicator in desc_lower:
                detected.append({
                    "name": template["name"],
                    "category": template["category"],
                    "threat_source": template["source"],
                    "likelihood": template["l"],
                    "impact": template["i"],
                    "vulnerabilities": [],
                })
        return detected

    def _build_executive_summary(
        self,
        name: str,
        description: str,
        sys_type: str,
        domain_key: str,
        stride_count: int,
        risk_count: int,
        risk_dist: dict[str, int],
        critical_high: int,
        overall_level: str,
        control_result: dict,
        risk_result: dict,
    ) -> dict:
        """Build a high-level executive summary."""
        top_risks = risk_result.get("risk_register", [])[:5]
        top_control_families = [
            c["control_id"] for c in control_result.get("controls", [])[:5]
        ]

        # Determine primary risk themes
        themes: list[str] = []
        if sys_type in ("web_app", "saas"):
            themes.extend(["Web application security", "Data protection", "Access control"])
        if sys_type == "infrastructure":
            themes.extend(["Infrastructure hardening", "Network security", "Cloud configuration"])
        if sys_type == "api":
            themes.extend(["API security", "Authentication", "Data in transit"])
        if domain_key == "healthcare":
            themes.extend(["PHI/PII protection", "AU Privacy Act compliance", "Clinical data integrity"])
        if domain_key == "finance":
            themes.extend(["Payment data security (PCI DSS)", "Financial fraud prevention", "KYC/AML"])
        if domain_key == "ecommerce":
            themes.extend(["Payment fraud prevention", "Customer data protection", "Transaction integrity"])
        if not themes:
            themes = ["Access control", "Data protection", "Incident response"]

        # Compliance readiness
        compliance_score = self._compliance_readiness(
            control_result.get("controls", []), domain_key
        )

        return {
            "system_assessed": name,
            "one_line_summary": f"{name} assessed as {overall_level} risk ({stride_count} STRIDE threats identified, {risk_count} risk entries scored).",
            "key_findings": [
                f"{critical_high} Critical/High risks require immediate attention.",
                f"Risk distribution: {', '.join(f'{k}: {v}' for k, v in risk_dist.items())}.",
                f"Top risk: {top_risks[0]['threat_name'] if top_risks else 'N/A'} (score {top_risks[0]['risk_score'] if top_risks else 0}).",
                f"Key control families: {', '.join(top_control_families)}.",
            ],
            "primary_themes": themes[:5],
            "overall_risk_level": overall_level,
            "risk_breakdown": risk_dist,
            "compliance_readiness": compliance_score,
            "immediate_actions": [
                r["recommendation"]
                for r in risk_result.get("recommendations", [])
                if r.get("priority") == "immediate"
            ][:5],
            "strategic_recommendations": [
                "Implement top-priority NIST 800-53 controls for identified risk areas",
                "Establish continuous monitoring (RMF Step 6) for ongoing threat detection",
                "Conduct penetration testing to validate threat model findings",
                "Review and update incident response plan aligned with identified scenarios",
            ],
        }

    @staticmethod
    def _compliance_readiness(controls: list[dict], domain_key: str) -> dict:
        """Estimate compliance readiness based on control coverage."""
        covered = {c["control_id"] for c in controls}

        if domain_key == "healthcare":
            required = {"AC-2", "AU-2", "AU-6", "IA-2", "SC-7", "SC-8", "SI-2"}
            standard = "Australian Privacy Act + APPs"
        elif domain_key == "finance":
            required = {"AC-2", "AU-2", "AU-6", "IA-2", "IA-5", "SC-7", "SC-8", "IR-4"}
            standard = "PCI DSS 4.0 / APRA CPS 234"
        elif domain_key == "saas":
            required = {"AC-2", "AU-2", "IA-2", "SC-7", "SI-2", "RA-5"}
            standard = "SOC 2 Type II / ISO 27001"
        else:
            required = {"AC-2", "AU-2", "IA-2", "SC-7", "SI-2"}
            standard = "NIST CSF 2.0 / ISO 27001"

        met = covered & required
        pct = round((len(met) / len(required)) * 100) if required else 0

        return {
            "standard": standard,
            "required_controls": len(required),
            "controls_addressed": len(met),
            "coverage_pct": pct,
            "status": "Strong" if pct >= 80 else "Moderate" if pct >= 50 else "Needs Attention",
            "gaps": sorted(required - covered),
        }


# ══════════════════════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════════════════════

def _stride_relevance(letter: str, asset_type: str, protocol: str, data_type: str, crosses_boundary: bool) -> int:
    """Score STRIDE relevance (0-5) for a data flow context."""
    score = 0
    at_lower = asset_type.lower()

    if letter == "S" and any(k in at_lower for k in ["web", "api", "auth", "login", "user"]):
        score += 3
    if letter == "T" and any(k in at_lower for k in ["web", "api", "database", "form", "file"]):
        score += 3
    if letter == "R" and any(k in at_lower for k in ["financial", "transaction", "payment", "compliance", "audit"]):
        score += 3
    if letter == "I" and any(k in at_lower for k in ["web", "api", "database", "pii", "healthcare", "clinic"]):
        score += 4
    if letter == "D" and any(k in at_lower for k in ["web", "api", "public", "saas", "ecommerce"]):
        score += 3
    if letter == "E" and any(k in at_lower for k in ["web", "api", "admin", "role", "privilege"]):
        score += 3

    if crosses_boundary:
        score += 2
    if protocol.upper() not in ("HTTPS", "SSH", "SFTP"):
        score += 2  # Unencrypted
    if data_type and data_type.lower() in ("pii", "phi", "payment", "credential", "health"):
        score += 2

    return min(5, score)


def _likelihood_hint(letter: str, asset_type: str) -> int:
    """Default likelihood hint based on STRIDE category and asset type."""
    at_lower = asset_type.lower()
    public_facing = any(k in at_lower for k in ["web", "saas", "public", "api"])
    if public_facing:
        return 4 if letter in ("S", "T", "D") else 3
    return 3 if letter in ("S", "T", "I") else 2


def _auto_likelihood(category: str, threat_source: str, vulnerabilities: list) -> int:
    """Auto-compute likelihood from context."""
    base = 3  # Moderate default
    if category in ("S", "T"):
        base = 4
    if category == "D":
        base = 3
    if category in ("R", "I", "E"):
        base = 3
    if "insider" in threat_source.lower():
        base = min(5, base + 1)
    if "apt" in threat_source.lower():
        base = 5
    if len(vulnerabilities) > 3:
        base = min(5, base + 1)
    return base


def _auto_impact(category: str, impact_context: str) -> int:
    """Auto-compute impact from context."""
    ctx_lower = (impact_context or "").lower()
    if any(k in ctx_lower for k in ["pii", "health", "payment", "financial", "breach"]):
        return 5
    if any(k in ctx_lower for k in ["customer", "user data", "confidential"]):
        return 4
    if any(k in ctx_lower for k in ["availability", "downtime", "service"]):
        return 3
    if category == "I":
        return 4  # Information disclosure is typically high impact
    return 3


def _map_controls(category: str, vulnerabilities: list) -> list[dict]:
    """Map threat category → NIST 800-53 controls."""
    controls = STRIDE_CONTROLS.get(category, [])
    result = []
    for ctrl in controls:
        result.append({
            "control_id": ctrl,
            "name": CONTROLS[ctrl]["name"],
            "description": CONTROLS[ctrl]["description"],
        })
    # Add vulnerability-specific controls
    vuln_lower = " ".join(vulnerabilities).lower()
    if "sql" in vuln_lower or "injection" in vuln_lower:
        result.append({"control_id": "SI-10", "name": "Information Input Validation", "description": "Validate inputs"})
    if "xss" in vuln_lower or "cross-site" in vuln_lower:
        result.append({"control_id": "SI-10", "name": "Information Input Validation", "description": "Validate inputs"})
    if "patch" in vuln_lower or "unpatched" in vuln_lower:
        result.append({"control_id": "SI-2", "name": "Flaw Remediation", "description": "Remediate system flaws"})
    return result


def _csf_mapping(category: str) -> list[str]:
    """Map STRIDE category → CSF 2.0 categories."""
    mapping = {
        "S": ["PR.AC", "ID.RA"],
        "T": ["PR.DS", "DE.AE"],
        "R": ["DE.AE", "RS.AN"],
        "I": ["PR.DS", "PR.PS", "ID.RA"],
        "D": ["PR.IR", "DE.CM"],
        "E": ["PR.AC", "ID.RA"],
    }
    return mapping.get(category, ["ID.RA"])


def _stride_name(letter: str) -> str:
    return STRIDE.get(letter, {}).get("name", letter) if letter in STRIDE else letter


def _count_by(items: list, key: str) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        k = item.get(key, "Unknown")
        counts[k] += 1
    return dict(counts)


def _build_heatmap(risk_register: list) -> dict:
    """Build a 5×5 risk heat-map (likelihood × impact)."""
    matrix = [[0]*5 for _ in range(5)]
    colors = [[0]*5 for _ in range(5)]
    for r in risk_register:
        l = r.get("likelihood", {}).get("value", 3) - 1
        i = r.get("impact", {}).get("value", 3) - 1
        if 0 <= l < 5 and 0 <= i < 5:
            matrix[l][i] += 1
            colors[l][i] = max(colors[l][i], l + i + 2)
    return {
        "matrix": matrix,
        "colors": colors,
        "labels": {"x": "Impact →", "y": "Likelihood →"},
        "max_value": max(max(row) for row in matrix) if any(matrix) else 0,
    }


def _generate_recommendations(critical: list, moderate: list, controls: list) -> list[dict]:
    """Generate prioritized recommendations from risk assessment."""
    recs = []
    for r in sorted(critical + moderate, key=lambda x: x.get("risk_score", 0), reverse=True)[:10]:
        recs.append({
            "priority": "immediate" if r["risk_level"] == "Critical" else "short_term",
            "risk_id": r["risk_id"],
            "threat": r["threat_name"],
            "recommendation": f"Address {r['threat_name']} via controls: {', '.join(c['control_id'] for c in r.get('relevant_controls', [])[:3])}",
            "residual_target": "Reduce to Moderate or below",
        })
    return recs


def _controls_by_family(controls: list[str]) -> dict[str, list[str]]:
    families: dict[str, list[str]] = defaultdict(list)
    for ctrl in controls:
        family = re.match(r"[A-Z]{2}", ctrl)
        if family:
            families[family.group()].append(ctrl)
    return dict(families)


def _build_markdown_report(
    organisation, scope, assessor, classification,
    summary, critical, moderate, low, top_risks,
    recommendations, heatmap, all_controls, timestamp,
) -> str:
    """Generate a board-ready markdown report."""
    lines = [
        f"# Threat & Risk Assessment Report",
        f"",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| **Organisation** | {organisation or 'Not specified'} |",
        f"| **Scope** | {scope or 'Not specified'} |",
        f"| **Assessor** | {assessor} |",
        f"| **Classification** | {classification} |",
        f"| **Date** | {timestamp[:10]} |",
        f"| **Framework** | NIST SP 800-30 Rev.1 / SP 800-37 Rev.2 / SP 800-53 Rev.5 |",
        f"",
        f"## Executive Summary",
        f"",
        f"- **Total Risks Assessed:** {summary.get('total_risks', 0)}",
        f"- **Overall Risk Level:** {summary.get('overall_risk_level', 'N/A')}",
        f"- **Critical/High Risks:** {summary.get('critical_high_count', 0)}",
        f"- **Control Families Addressed:** {len(all_controls)}",
        f"",
        f"### Risk Distribution",
        f"",
        f"| Level | Count |",
        f"|-------|-------|",
    ]
    for level, count in summary.get("risk_distribution", {}).items():
        lines.append(f"| {level} | {count} |")

    lines.extend([
        "",
        f"## Top 10 Risks",
        f"",
        f"| Rank | ID | Threat | Score | Level |",
        f"|------|----|--------|-------|-------|",
    ])
    for i, r in enumerate(top_risks):
        lines.append(f"| {i+1} | {r['risk_id']} | {r['threat_name'][:40]} | {r['risk_score']} | {r['risk_level']} |")

    lines.extend([
        "",
        "## Critical & High Risks — Detailed",
        "",
    ])
    for r in critical:
        lines.extend([
            f"### {r['risk_id']}: {r['threat_name']}",
            f"- **Risk Score:** {r['risk_score']} ({r['risk_level']})",
            f"- **Likelihood:** {r['likelihood']['label']} | **Impact:** {r['impact']['label']}",
            f"- **Relevant Controls:** {', '.join(c['control_id'] for c in r.get('relevant_controls', []))}",
            "",
        ])

    lines.extend([
        "## Recommendations",
        "",
        "| Priority | Risk | Recommendation |",
        "|----------|------|----------------|",
    ])
    for rec in recommendations:
        lines.append(f"| {rec['priority']} | {rec['threat'][:30]} | {rec['recommendation'][:60]}... |")

    lines.extend([
        "",
        "## NIST CSF Mapping",
        "",
        f"Control families addressed: {', '.join(sorted(all_controls))}",
        "",
        "---",
        f"*Generated by MoiraiCore Threat Engine | {timestamp}*",
    ])

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    engine = ThreatRiskEngine()

    if len(sys.argv) < 2:
        print("Usage: python3 threat_mod_toolkit.py <command> [json_arg]")
        print("Commands: stride, risk, auto, report, bowtie")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "auto":
        description = sys.argv[2] if len(sys.argv) > 2 else ""
        system_name = sys.argv[3] if len(sys.argv) > 3 else ""
        result = engine.auto_assess(description, system_name)
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "report":
        assessment_json = sys.argv[2] if len(sys.argv) > 2 else "{}"
        try:
            assessment = json.loads(assessment_json)
        except json.JSONDecodeError:
            assessment = {}
        result = engine.executive_report(assessment)
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "bowtie":
        threat_name = sys.argv[2] if len(sys.argv) > 2 else ""
        asset = sys.argv[3] if len(sys.argv) > 3 else ""
        result = engine.bowtie_analysis(threat_name, asset)
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "stride":
        asset_name = sys.argv[2] if len(sys.argv) > 2 else "System"
        # Read JSON config from stdin if available
        config = {}
        if not sys.stdin.isatty():
            try:
                config = json.loads(sys.stdin.read())
            except json.JSONDecodeError:
                pass
        result = engine.stride_model(
            asset_name=asset_name,
            data_flows=config.get("data_flows", []),
            trust_boundaries=config.get("trust_boundaries", []),
            entry_points=config.get("entry_points", []),
            assets=config.get("assets", []),
        )
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "risk":
        # Expect JSON with threats list on stdin or as arg
        raw = sys.argv[2] if len(sys.argv) > 2 else "{}"
        try:
            parsed = json.loads(raw)
            threats = parsed.get("threats", [])
        except json.JSONDecodeError:
            threats = []
        risk_level = sys.argv[3] if len(sys.argv) > 3 else "Moderate"
        result = engine.risk_assess(threats)
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "controls":
        print(json.dumps({"error": "controls command not available via CLI. Use the API endpoint POST /api/threat/controls"}))

    elif cmd == "residual":
        print(json.dumps({"error": "residual command not available via CLI. Use the API endpoint POST /api/threat/residual"}))

    elif cmd == "catalogue":
        print(json.dumps({"error": "catalogue command not available via CLI. Use the API endpoint POST /api/threat/catalogue"}))

    elif cmd == "rmf":
        print(json.dumps({"error": "rmf command not available via CLI. Use the API endpoint POST /api/threat/rmf"}))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
