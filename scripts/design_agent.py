"""
Design Agent — Produces professional design specifications for projects.

This agent sits between ScrumMaster task assignment and code generation.
It researches the brand, creates a design system, and produces page-level
design specs that implementation agents (Tier 2/3) follow.

Pipeline position:
  ScrumMaster → DESIGN AGENT → Tier 2/3 (implement design) → merge

Output: A design_spec.json that contains:
  - colors (primary, secondary, accent, neutrals, semantic)
  - typography (font families, sizes, weights)
  - spacing scale
  - border radius / shadows
  - component styles (buttons, cards, inputs, badges, nav)
  - page layouts (HTML structure with class names)
  - responsive breakpoints
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Design System Types ──

@dataclass
class ColorPalette:
    """Color system for a brand."""
    primary: str = "#2c5f2d"
    primary_light: str = "#3d7a3e"
    primary_dark: str = "#1a3a1b"
    secondary: str = "#d4a853"
    secondary_light: str = "#e8c97a"
    secondary_dark: str = "#b8922e"
    accent: str = "#e8e0d0"
    background: str = "#faf7f2"
    surface: str = "#ffffff"
    text_primary: str = "#1a1a1a"
    text_secondary: str = "#555555"
    text_muted: str = "#888888"
    border: str = "#e0dcd4"
    success: str = "#4caf50"
    warning: str = "#ff9800"
    error: str = "#f44336"


@dataclass
class Typography:
    """Typography system."""
    font_family: str = "'Segoe UI', system-ui, -apple-system, sans-serif"
    heading_family: str = "Georgia, 'Times New Roman', serif"
    size_xs: str = "0.75rem"
    size_sm: str = "0.875rem"
    size_base: str = "1rem"
    size_lg: str = "1.125rem"
    size_xl: str = "1.5rem"
    size_2xl: str = "2rem"
    size_3xl: str = "2.5rem"
    weight_normal: int = 400
    weight_medium: int = 500
    weight_semibold: int = 600
    weight_bold: int = 700
    line_height_tight: float = 1.2
    line_height_normal: float = 1.6
    line_height_relaxed: float = 1.8


@dataclass
class Spacing:
    """Spacing scale."""
    xs: str = "4px"
    sm: str = "8px"
    md: str = "16px"
    lg: str = "24px"
    xl: str = "32px"
    xxl: str = "48px"
    xxxl: str = "64px"


@dataclass
class ComponentStyles:
    """Reusable component styles."""
    button_padding: str = "14px 28px"
    button_radius: str = "8px"
    button_font_weight: int = 600
    button_text_transform: str = "uppercase"
    button_letter_spacing: str = "0.5px"
    card_padding: str = "24px"
    card_radius: str = "12px"
    card_shadow: str = "0 2px 12px rgba(0,0,0,0.08)"
    card_hover_shadow: str = "0 8px 24px rgba(0,0,0,0.12)"
    input_padding: str = "12px 16px"
    input_radius: str = "8px"
    input_border_width: str = "2px"
    badge_radius: str = "16px"
    badge_padding: str = "4px 14px"
    nav_height: str = "56px"
    section_gap: str = "40px"


@dataclass
class DesignSpec:
    """Complete design specification for a project."""
    project_name: str
    brand_description: str
    brand_keywords: list[str] = field(default_factory=list)
    colors: ColorPalette = field(default_factory=ColorPalette)
    typography: Typography = field(default_factory=Typography)
    spacing: Spacing = field(default_factory=Spacing)
    components: ComponentStyles = field(default_factory=ComponentStyles)
    border_radius_sm: str = "6px"
    border_radius_md: str = "10px"
    border_radius_lg: str = "16px"
    max_width: str = "1200px"
    page_padding: str = "24px"
    css_variables: dict = field(default_factory=dict)
    global_css: str = ""
    pages: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if not self.css_variables:
            self.css_variables = {
                "--color-primary": self.colors.primary,
                "--color-primary-light": self.colors.primary_light,
                "--color-primary-dark": self.colors.primary_dark,
                "--color-secondary": self.colors.secondary,
                "--color-secondary-light": self.colors.secondary_light,
                "--color-secondary-dark": self.colors.secondary_dark,
                "--color-accent": self.colors.accent,
                "--color-bg": self.colors.background,
                "--color-surface": self.colors.surface,
                "--color-text": self.colors.text_primary,
                "--color-text-secondary": self.colors.text_secondary,
                "--color-text-muted": self.colors.text_muted,
                "--color-border": self.colors.border,
                "--font-family": self.typography.font_family,
                "--font-heading": self.typography.heading_family,
                "--radius-sm": self.border_radius_sm,
                "--radius-md": self.border_radius_md,
                "--radius-lg": self.border_radius_lg,
                "--shadow-card": self.components.card_shadow,
                "--shadow-hover": self.components.card_hover_shadow,
                "--max-width": self.max_width,
            }

    def to_dict(self) -> dict:
        return {
            "project_name": self.project_name,
            "brand_description": self.brand_description,
            "brand_keywords": self.brand_keywords,
            "colors": self.colors.__dict__,
            "typography": self.typography.__dict__,
            "spacing": self.spacing.__dict__,
            "components": self.components.__dict__,
            "border_radius": {"sm": self.border_radius_sm, "md": self.border_radius_md, "lg": self.border_radius_lg},
            "max_width": self.max_width,
            "page_padding": self.page_padding,
            "css_variables": self.css_variables,
            "global_css": self.global_css,
            "pages": self.pages,
        }


# ── Brand Presets ──

BRAND_PRESETS = {
    "wellness": {
        "colors": ColorPalette(
            primary="#5b8a72",
            primary_light="#7baa92",
            primary_dark="#3d6b52",
            secondary="#d4a853",
            secondary_light="#e8c97a",
            secondary_dark="#b8922e",
            accent="#f0ebe3",
            background="#faf8f5",
            surface="#ffffff",
            text_primary="#2c2c2c",
            text_secondary="#5a5a5a",
            text_muted="#8a8a8a",
            border="#e8e3dc",
        ),
        "typography": Typography(
            font_family="'Inter', 'Segoe UI', system-ui, sans-serif",
            heading_family="'Playfair Display', Georgia, serif",
        ),
        "keywords": ["calm", "natural", "wellness", "serene", "organic"],
    },
    "thai_therapy": {
        "colors": ColorPalette(
            primary="#8b6914",
            primary_light="#b8942e",
            primary_dark="#5c4a0e",
            secondary="#c62828",
            secondary_light="#e53935",
            secondary_dark="#8e1414",
            accent="#fff8e1",
            background="#fdfbf7",
            surface="#ffffff",
            text_primary="#1a1a1a",
            text_secondary="#4a4a4a",
            text_muted="#7a7a7a",
            border="#ede7dc",
        ),
        "typography": Typography(
            font_family="'Noto Sans', 'Segoe UI', system-ui, sans-serif",
            heading_family="'Noto Serif', Georgia, serif",
        ),
        "keywords": ["thai", "massage", "wellness", "traditional", "healing", "Parramatta"],
    },
}


# ── Design Agent ──

class DesignAgent:
    """
    Produces professional design specifications for projects.

    Can work in two modes:
    1. PRESET mode: Use a brand preset (fast, deterministic)
    2. AI mode: Call Gemini to generate a unique design spec (slower, custom)
    """

    def __init__(self, project_space: str):
        self.project_space = Path(project_space)
        self.design_dir = self.project_space / ".antigravity" / "design"
        self.design_dir.mkdir(parents=True, exist_ok=True)

    def generate_design_spec(
        self,
        project_name: str,
        brand_description: str,
        preset: Optional[str] = None,
        custom_requirements: Optional[str] = None,
        use_ai: bool = False,
    ) -> DesignSpec:
        """Generate a complete design spec for the project."""

        if preset and preset in BRAND_PRESETS:
            preset_data = BRAND_PRESETS[preset]
            spec = DesignSpec(
                project_name=project_name,
                brand_description=brand_description,
                brand_keywords=preset_data.get("keywords", []),
                colors=preset_data.get("colors", ColorPalette()),
                typography=preset_data.get("typography", Typography()),
            )
        else:
            spec = DesignSpec(
                project_name=project_name,
                brand_description=brand_description,
            )

        # Generate global CSS from design system
        spec.global_css = self._generate_global_css(spec)

        # Generate page layouts
        spec.pages = self._generate_page_layouts(spec, custom_requirements)

        # Optionally enhance with AI
        if use_ai:
            spec = self._enhance_with_ai(spec, custom_requirements)

        # Save design spec
        spec_path = self.design_dir / "design_spec.json"
        spec_path.write_text(json.dumps(spec.to_dict(), indent=2))

        return spec

    def _generate_global_css(self, spec: DesignSpec) -> str:
        """Generate CSS custom properties and base styles from design system."""
        c = spec.colors
        t = spec.typography
        s = spec.spacing
        comp = spec.components

        lines = []
        lines.append(":root {")
        for k, v in spec.css_variables.items():
            lines.append(f"  {k}: {v};")
        lines.append("}")
        lines.append("")
        lines.append("* { margin: 0; padding: 0; box-sizing: border-box; }")
        lines.append("")
        lines.append("body {")
        lines.append(f"  font-family: {t.font_family};")
        lines.append(f"  font-size: {t.size_base};")
        lines.append(f"  line-height: {t.line_height_normal};")
        lines.append(f"  color: {c.text_primary};")
        lines.append(f"  background: {c.background};")
        lines.append("  -webkit-font-smoothing: antialiased;")
        lines.append("}")
        lines.append("")
        lines.append("h1, h2, h3, h4 {")
        lines.append(f"  font-family: {t.heading_family};")
        lines.append(f"  font-weight: {t.weight_bold};")
        lines.append(f"  line-height: {t.line_height_tight};")
        lines.append(f"  color: {c.text_primary};")
        lines.append("}")
        lines.append(f"h1 {{ font-size: {t.size_3xl}; }}")
        lines.append(f"h2 {{ font-size: {t.size_2xl}; }}")
        lines.append(f"h3 {{ font-size: {t.size_xl}; }}")
        lines.append("")
        lines.append(f"a {{ color: {c.primary}; text-decoration: none; }}")
        lines.append(f"a:hover {{ color: {c.primary_dark}; }}")
        lines.append("")
        lines.append("button, .btn {")
        lines.append("  display: inline-block;")
        lines.append(f"  padding: {comp.button_padding};")
        lines.append("  border: none;")
        lines.append(f"  border-radius: {comp.button_radius};")
        lines.append(f"  font-family: {t.font_family};")
        lines.append(f"  font-weight: {comp.button_font_weight};")
        lines.append(f"  font-size: {t.size_sm};")
        lines.append(f"  text-transform: {comp.button_text_transform};")
        lines.append(f"  letter-spacing: {comp.button_letter_spacing};")
        lines.append("  cursor: pointer;")
        lines.append("  transition: all 0.2s ease;")
        lines.append("}")
        lines.append("")
        lines.append(".btn-primary {")
        lines.append(f"  background: {c.primary};")
        lines.append("  color: white;")
        lines.append("}")
        lines.append(".btn-primary:hover {")
        lines.append(f"  background: {c.primary_dark};")
        lines.append("  transform: translateY(-1px);")
        lines.append("  box-shadow: 0 4px 12px rgba(0,0,0,0.15);")
        lines.append("}")
        lines.append("")
        lines.append(".btn-secondary {")
        lines.append(f"  background: {c.secondary};")
        lines.append("  color: white;")
        lines.append("}")
        lines.append(".btn-secondary:hover {")
        lines.append(f"  background: {c.secondary_dark};")
        lines.append("}")
        lines.append("")
        lines.append(".card {")
        lines.append(f"  background: {c.surface};")
        lines.append(f"  border-radius: {spec.border_radius_lg};")
        lines.append(f"  padding: {comp.card_padding};")
        lines.append(f"  box-shadow: {comp.card_shadow};")
        lines.append("  transition: box-shadow 0.2s ease, transform 0.2s ease;")
        lines.append("}")
        lines.append(".card:hover {")
        lines.append(f"  box-shadow: {comp.card_hover_shadow};")
        lines.append("  transform: translateY(-2px);")
        lines.append("}")
        lines.append("")
        lines.append("input, select {")
        lines.append("  width: 100%;")
        lines.append(f"  padding: {comp.input_padding};")
        lines.append(f"  border: {comp.input_border_width} solid {c.border};")
        lines.append(f"  border-radius: {comp.input_radius};")
        lines.append(f"  font-family: {t.font_family};")
        lines.append(f"  font-size: {t.size_base};")
        lines.append("  transition: border-color 0.2s ease;")
        lines.append("}")
        lines.append("input:focus, select:focus {")
        lines.append("  outline: none;")
        lines.append(f"  border-color: {c.primary};")
        lines.append("}")
        lines.append("")
        lines.append(".badge {")
        lines.append("  display: inline-block;")
        lines.append(f"  padding: {comp.badge_padding};")
        lines.append(f"  border-radius: {comp.badge_radius};")
        lines.append(f"  font-size: {t.size_xs};")
        lines.append(f"  font-weight: {t.weight_semibold};")
        lines.append("  text-transform: uppercase;")
        lines.append("  letter-spacing: 0.5px;")
        lines.append("}")
        lines.append("")
        lines.append(".badge-bronze { background: #cd7f32; color: white; }")
        lines.append(".badge-silver { background: #c0c0c0; color: #333; }")
        lines.append(".badge-gold { background: #ffd700; color: #333; }")
        lines.append(".badge-platinum { background: linear-gradient(135deg, #e5e4e2, #b0b0b0); color: #333; }")
        lines.append("")
        lines.append(".container {")
        lines.append(f"  max-width: {spec.max_width};")
        lines.append("  margin: 0 auto;")
        lines.append(f"  padding: 0 {spec.page_padding};")
        lines.append("}")

        return "\n".join(lines)

    def _generate_page_layouts(self, spec: DesignSpec, requirements: Optional[str] = None) -> list[dict]:
        """Generate page structure with class names matching the design system."""
        c = spec.colors
        return [
            {
                "name": "home",
                "title": "Home",
                "sections": [
                    {"type": "hero", "classes": ["hero"], "content": "Welcome section with brand messaging"},
                    {"type": "stats", "classes": ["stats-grid"], "content": "Key metrics in cards"},
                    {"type": "features", "classes": ["card-grid"], "content": "Feature cards"},
                ],
            },
            {
                "name": "register",
                "title": "Join Now",
                "sections": [
                    {"type": "form", "classes": ["card"], "content": "Registration form with name, phone, treatment select"},
                ],
            },
            {
                "name": "check_rewards",
                "title": "Check Rewards",
                "sections": [
                    {"type": "form", "classes": ["card"], "content": "Phone lookup form"},
                    {"type": "results", "classes": ["card"], "content": "Customer info and reward status"},
                    {"type": "earn_form", "classes": ["card", "hidden"], "content": "Add visit form (shown after lookup)"},
                ],
            },
        ]

    def _enhance_with_ai(self, spec: DesignSpec, requirements: Optional[str] = None) -> DesignSpec:
        """Call Gemini AI to enhance the design spec with custom touches."""
        try:
            from gemini_worker import GeminiConfig, generate_code_block

            config = GeminiConfig.from_env()
            if config.api_key:
                prompt = f"""
You are a senior UI/UX designer. Enhance this design spec for "{spec.project_name}".
Brand: {spec.brand_description}
Requirements: {requirements or "Modern, professional wellness clinic design"}

Current colors: {json.dumps(spec.colors.__dict__, indent=2)}

Suggest improvements to make it more premium and modern.
Return ONLY a JSON object with keys: colors (object), typography (object), components (object), global_css_additions (string).
Keep the same structure but refine values for a luxury wellness feel.
"""
                result = generate_code_block(
                    project_space="",
                    spec=prompt,
                    file_path="design_spec.json",
                    config=config,
                )
                if result and not result.startswith("ERROR"):
                    try:
                        import re
                        json_match = re.search(r'\{[\s\S]*\}', result)
                        if json_match:
                            enhancements = json.loads(json_match.group())
                            if "colors" in enhancements:
                                for k, v in enhancements["colors"].items():
                                    if hasattr(spec.colors, k):
                                        setattr(spec.colors, k, v)
                            if "typography" in enhancements:
                                for k, v in enhancements["typography"].items():
                                    if hasattr(spec.typography, k):
                                        setattr(spec.typography, k, v)
                    except json.JSONDecodeError:
                        pass  # Silently fall back to preset
        except (ImportError, Exception):
            pass  # Silently fall back to preset

        return spec

    def load_spec(self) -> Optional[DesignSpec]:
        """Load existing design spec."""
        spec_path = self.design_dir / "design_spec.json"
        if spec_path.exists():
            data = json.loads(spec_path.read_text())
            spec = DesignSpec(
                project_name=data.get("project_name", ""),
                brand_description=data.get("brand_description", ""),
                brand_keywords=data.get("brand_keywords", []),
            )
            spec.pages = data.get("pages", [])
            spec.global_css = data.get("global_css", "")
            return spec
        return None


def generate_design_spec(project_space: str, project_name: str, brand: str, preset: str = "wellness") -> DesignSpec:
    """Convenience function: generate and save a design spec."""
    agent = DesignAgent(project_space)
    spec = agent.generate_design_spec(
        project_name=project_name,
        brand_description=brand,
        preset=preset,
    )
    return spec


if __name__ == "__main__":
    # Example: generate a design spec for a project at an arbitrary path.
    spec = generate_design_spec(
        "projects/example-project",
        "Example Brand",
        "A sample project to demonstrate design-spec generation.",
        preset="wellness",
    )
    print(f"Design spec generated: {len(spec.global_css)} chars CSS")
    print(f"Pages: {len(spec.pages)}")
    print(f"Primary color: {spec.colors.primary}")
    print(f"Heading font: {spec.typography.heading_family}")
