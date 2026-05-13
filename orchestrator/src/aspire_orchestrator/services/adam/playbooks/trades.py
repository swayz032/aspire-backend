"""TRADES Playbooks — 6 research playbooks for trades ICP.

Segments: plumbers, HVAC, electricians, roofers, painters, GCs, landscapers
Playbooks: Property Facts & Permits, Estimate Research, Tool/Material Price Check,
           Competitor Pricing Scan, Subcontractor Scout, Territory Opportunity Scan
"""

from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import random as _random
import re
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.services.adam.schemas.business_record import SourceAttribution
from aspire_orchestrator.services.adam.schemas.playbook_context import PlaybookContext
from aspire_orchestrator.services.adam.schemas.research_response import ResearchResponse
from aspire_orchestrator.services.adam.verifier import verify_records

logger = logging.getLogger(__name__)
