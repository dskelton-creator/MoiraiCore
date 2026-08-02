
# ── Pipeline Definitions ──────────────────────────────────────────

PIPELINE_DEFINITIONS = {
    "seo-content": {
        "name": "SEO Content Pipeline",
        "description": "Research → SEO Brief → Write → SEO Review → Synthesize",
        "stages": [
            {
                "name": "research",
                "agent": "researcher",
                "prompt": "Research the following topic thoroughly for SEO content creation.\n\nTopic: {topic}\nTarget Keyword: {keyword}\nAudience: {audience}\n\nFind:\n- Top 10 ranking articles for the target keyword\n- Key subtopics and questions people ask\n- Content gaps in existing articles\n- Recommended word count and structure\n- Related long-tail keywords\n\nBe thorough. Save findings to agents/researcher/findings/{slug}-research.md",
                "skills": ["arxiv", "blogwatcher"],
                "timeout": 300,
                "expected_criteria": ["keyword", "competitor", "content"],
            },
            {
                "name": "seo-brief",
                "agent": "seo",
                "prompt": "Create an SEO content brief based on research findings.\n\nTopic: {topic}\nTarget Keyword: {keyword}\nResearch: {research_output}\n\nProvide:\n- Primary and secondary keywords\n- Recommended title tag (under 60 chars)\n- Meta description (under 155 chars)\n- Heading structure (H1-H3)\n- Internal and external link suggestions\n- Schema markup recommendation\n- Target word count: {word_count}\n- Readability target",
                "skills": ["seo-toolkit"],
                "timeout": 180,
                "expected_criteria": ["title", "meta", "heading", "keyword"],
            },
            {
                "name": "write-draft",
                "agent": "writer",
                "prompt": "Write an SEO-optimized article following this brief.\n\nSEO Brief: {seo_brief_output}\n\nRequirements:\n- Conversational but professional tone\n- Australian English spelling\n- Include all recommended keywords naturally\n- Follow the heading structure exactly\n- Add internal links where relevant\n- Word count: {word_count}\n- Use markdown formatting",
                "skills": ["writing-plans", "humanizer"],
                "timeout": 300,
                "expected_criteria": ["introduction", "conclusion", "heading"],
            },
            {
                "name": "seo-review",
                "agent": "seo",
                "prompt": "Review this article draft against SEO best practices.\n\nDraft: {write_draft_output}\n\nScore out of 100 and check:\n- Keyword density and placement\n- Meta elements (title, description)\n- Heading hierarchy\n- Internal/external links\n- Image alt text suggestions\n- Schema markup completeness\n- Readability score\n\nProvide specific improvement recommendations.",
                "skills": ["seo-toolkit"],
                "timeout": 180,
                "expected_criteria": ["score", "recommendation"],
            },
        ],
        "synthesis": {
            "prompt": "Synthesize all stage outputs into a final SEO content package.\n\nResearch: {research_output}\nSEO Brief: {seo_brief_output}\nArticle Draft: {write_draft_output}\nSEO Review: {seo_review_output}\n\nProduce:\n1. Executive summary of the content strategy\n2. Final article (incorporating SEO review recommendations)\n3. Meta tags (title + description)\n4. Schema markup\n5. Internal linking plan\n6. Content promotion suggestions",
        },
    },
    "feature-dev": {
        "name": "Feature Development Pipeline",
        "description": "Requirements → Security → Implement → Review → Synthesize",
        "stages": [
            {
                "name": "requirements",
                "agent": "researcher",
                "prompt": "Research and define requirements for this feature.\n\nFeature: {feature_name}\nDescription: {description}\n\nFind:\n- User pain points this solves\n- Existing solutions and their limitations\n- Technical approaches/architecture options\n- Known security considerations\n- Estimation of complexity",
                "timeout": 300,
                "expected_criteria": ["requirement", "approach", "complexity"],
            },
            {
                "name": "security-assessment",
                "agent": "threat",
                "prompt": "Conduct a security assessment for this feature.\n\nRequirements: {requirements_output}\n\nProvide:\n- STRIDE threat model\n- Data flow with trust boundaries\n- Risk assessment (likelihood x impact)\n- Recommended security controls\n- Compliance considerations (ISO 27001, AU Privacy Act)",
                "skills": ["security-review-docker"],
                "timeout": 300,
                "expected_criteria": ["threat", "risk", "control"],
            },
            {
                "name": "implement",
                "agent": "developer",
                "prompt": "Implement the feature according to requirements and security guidance.\n\nRequirements: {requirements_output}\nSecurity: {security_assessment_output}\n\nUse TDD approach:\n1. Write tests first\n2. Implement the feature\n3. Run tests\n4. Security self-review\n\nFollow existing codebase patterns.",
                "skills": ["systematic-debugging", "test-driven-development"],
                "timeout": 600,
                "expected_criteria": ["test", "implementation"],
            },
            {
                "name": "security-review",
                "agent": "threat",
                "prompt": "Review the implemented code for security issues.\n\nImplementation: {implement_output}\nPrevious threat model: {security_assessment_output}\n\nCheck for:\n- OWASP Top 10 vulnerabilities\n- Input validation gaps\n- Authentication/authorization issues\n- Data exposure risks\n- Dependency vulnerabilities",
                "skills": ["security-review-docker"],
                "timeout": 300,
                "expected_criteria": ["vulnerability", "recommendation"],
            },
        ],
        "synthesis": {
            "prompt": "Synthesize all stage outputs into a final feature delivery report.\n\nRequirements: {requirements_output}\nSecurity Assessment: {security_assessment_output}\nImplementation: {implement_output}\nSecurity Review: {security_review_output}\n\nProduce:\n1. Executive summary\n2. Feature overview and architecture\n3. Security posture summary\n4. Test results\n5. Deployment checklist\n6. Known risks and mitigations",
        },
    },
    "competitor-analysis": {
        "name": "Competitor Analysis Pipeline",
        "description": "Research → SEO Compare → Analysis → Synthesize",
        "stages": [
            {
                "name": "competitor-research",
                "agent": "researcher",
                "prompt": "Conduct a comprehensive competitor analysis.\n\nTarget: {company_name} ({company_url})\nCompetitors: {competitor_list}\n\nFor each competitor find:\n- Business model and value proposition\n- Key features and differentiators\n- Pricing structure (if public)\n- Target audience\n- Strengths and weaknesses\n- Recent news/developments",
                "timeout": 300,
                "expected_criteria": ["competitor", "strength", "weakness"],
            },
            {
                "name": "seo-comparison",
                "agent": "seo",
                "prompt": "Conduct a head-to-head SEO comparison.\n\nOur site: {company_url}\nCompetitors: {competitor_urls}\n\nCompare:\n- Domain authority and backlink profiles\n- Keyword overlap and gaps\n- Content strategy and publishing frequency\n- Technical SEO (speed, mobile, structure)\n- Featured snippets and SERP features",
                "skills": ["seo-toolkit"],
                "timeout": 300,
                "expected_criteria": ["keyword", "authority", "content"],
            },
            {
                "name": "analysis-report",
                "agent": "writer",
                "prompt": "Synthesize the research and SEO data into actionable recommendations.\n\nResearch: {competitor_research_output}\nSEO Data: {seo_comparison_output}\n\nProduce:\n- Competitive positioning map\n- SWOT analysis\n- Top 10 actionable recommendations\n- Quick wins vs long-term strategy\n- Content gap opportunities",
                "skills": ["writing-plans"],
                "timeout": 300,
                "expected_criteria": ["swot", "recommendation", "opportunity"],
            },
        ],
        "synthesis": {
            "prompt": "Create the final competitor analysis report.\n\nResearch: {competitor_research_output}\nSEO Comparison: {seo_comparison_output}\nAnalysis: {analysis_report_output}\n\nProduce:\n1. Executive summary\n2. Competitive landscape overview\n3. Detailed competitor profiles\n4. SEO gap analysis\n5. Strategic recommendations (prioritized)\n6. 90-day action plan",
        },
    },
}
