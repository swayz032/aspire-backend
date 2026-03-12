"""Aspire Universal Agent Templates.

This package contains template files for creating new Aspire agents:
  - AgentMemoryMixin: 3-tier agentic memory (working/episodic/semantic)
  - AgenticSkillPack: Base class with memory + bounded multi-step reasoning
  - Template files: persona, manifest, risk policy, test scaffold

Usage:
    from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack

    class MyAgentSkillPack(AgenticSkillPack):
        def __init__(self):
            super().__init__(agent_id="my-agent", agent_name="My Agent")
"""
