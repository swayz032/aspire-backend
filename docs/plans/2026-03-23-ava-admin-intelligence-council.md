# Ava Admin Intelligence Upgrade + Council Production Wiring

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give Ava Admin tools to query every data source the admin portal uses, and wire the Meeting of Minds council for real multi-model API calls with persistent state.

**Architecture:** Two workstreams: (1) Add 10 new desk methods to `ava_admin_desk.py` + wire through `ava_admin.py`, each querying the same Supabase tables/RPCs the admin portal pages use. (2) Replace council in-memory state with Supabase persistence, add real multi-model LLM calls (OpenAI, Google, Anthropic), and LLM-powered adjudication.

**Tech Stack:** Python 3.11, FastAPI, Supabase (supabase_select/supabase_rpc), OpenAI API, Google Generative AI API, Anthropic API, pytest + AsyncMock

---

## Workstream 1: Ava Admin Data Tools

### Task 1: Provider Call Logs Tool

**Files:**
- Modify: `backend/orchestrator/src/aspire_orchestrator/skillpacks/ava_admin_desk.py` (append after method 14)
- Modify: `backend/orchestrator/src/aspire_orchestrator/skillpacks/ava_admin.py` (add wrapper)
- Modify: `backend/orchestrator/tests/test_ava_admin.py` (add test)

**Step 1: Write the failing test**

Add to `tests/test_ava_admin.py`:

```python
@pytest.mark.asyncio
async def test_get_provider_call_logs() -> None:
    """Query provider_call_log table via desk method."""
    mock_rows = [
        {"id": "pcl-1", "provider": "stripe", "status": "success", "created_at": "2026-03-23T00:00:00Z"},
        {"id": "pcl-2", "provider": "stripe", "status": "error", "error_code": "rate_limit", "created_at": "2026-03-23T00:01:00Z"},
    ]
    with patch("aspire_orchestrator.skillpacks.ava_admin_desk.supabase_select", new_callable=AsyncMock, return_value=mock_rows):
        pack = AvaAdminSkillPack()
        ctx = _make_ctx()
        result = await pack.admin_ops_provider_call_logs({"provider": "stripe", "limit": 50}, ctx)
        assert result.success is True
        assert result.data["count"] == 2
        assert result.data["calls"][0]["provider"] == "stripe"
```

**Step 2: Run test to verify it fails**

Run: `cd backend/orchestrator && python -m pytest tests/test_ava_admin.py::test_get_provider_call_logs -v`
Expected: FAIL — `admin_ops_provider_call_logs` not defined

**Step 3: Implement desk method**

Add to `ava_admin_desk.py` after method 14 (get_metrics_snapshot), before the singleton:

```python
    # =========================================================================
    # 15. Provider Call Logs (GREEN — read-only Supabase query)
    # =========================================================================

    async def get_provider_call_logs(
        self,
        ctx: AgentContext,
        *,
        provider: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> AgentResult:
        """Query provider_call_log table — same data as /provider-call-log page."""
        try:
            from aspire_orchestrator.services.supabase_client import supabase_select
            filters: dict[str, Any] = {}
            if provider:
                filters["provider"] = f"eq.{provider}"
            if status:
                filters["status"] = f"eq.{status}"
            rows = await supabase_select(
                "provider_call_log", filters, order_by="created_at.desc", limit=limit,
            )
        except Exception as e:
            return AgentResult(success=False, error=f"Provider call logs query failed: {e}")

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.provider_call_logs",
            status="ok",
            inputs={"provider": provider, "status": status, "limit": limit},
            metadata={"count": len(rows)},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"calls": rows, "count": len(rows), "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )
```

Add wrapper to `ava_admin.py`:

```python
    async def admin_ops_provider_call_logs(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        provider = params.get('provider')
        status = params.get('status')
        limit = int(params.get('limit', 50))
        return await self._desk.get_provider_call_logs(ctx, provider=provider, status=status, limit=limit)
```

Also add `from aspire_orchestrator.services.supabase_client import supabase_select` to the imports at top of desk file (it's already used by methods 9-10 via lazy import, but we should add it to make it consistent — OR keep lazy import pattern to match existing code). **Decision: keep lazy import pattern to match existing methods 9 and 10.**

**Step 4: Run test to verify it passes**

Run: `cd backend/orchestrator && python -m pytest tests/test_ava_admin.py::test_get_provider_call_logs -v`
Expected: PASS

**Step 5: Commit**

```bash
cd backend/orchestrator
git add src/aspire_orchestrator/skillpacks/ava_admin_desk.py src/aspire_orchestrator/skillpacks/ava_admin.py tests/test_ava_admin.py
git commit -m "feat(admin): add provider call logs tool to Ava Admin desk"
```

---

### Task 2: Client Events Tool

**Files:**
- Modify: `backend/orchestrator/src/aspire_orchestrator/skillpacks/ava_admin_desk.py`
- Modify: `backend/orchestrator/src/aspire_orchestrator/skillpacks/ava_admin.py`
- Modify: `backend/orchestrator/tests/test_ava_admin.py`

**Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_get_client_events() -> None:
    """Query client_events table via desk method."""
    mock_rows = [
        {"id": "evt-1", "event_type": "page_view", "severity": "info", "created_at": "2026-03-23T00:00:00Z"},
    ]
    with patch("aspire_orchestrator.skillpacks.ava_admin_desk.supabase_select", new_callable=AsyncMock, return_value=mock_rows):
        pack = AvaAdminSkillPack()
        ctx = _make_ctx()
        result = await pack.admin_ops_client_events({"event_type": "page_view", "limit": 50}, ctx)
        assert result.success is True
        assert result.data["count"] == 1
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

Desk method:

```python
    # =========================================================================
    # 16. Client Events (GREEN — read-only Supabase query)
    # =========================================================================

    async def get_client_events(
        self,
        ctx: AgentContext,
        *,
        event_type: str | None = None,
        severity: str | None = None,
        limit: int = 50,
    ) -> AgentResult:
        """Query client_events table — same data as /client-events and /frontend-health pages."""
        try:
            from aspire_orchestrator.services.supabase_client import supabase_select
            filters: dict[str, Any] = {}
            if event_type:
                filters["event_type"] = f"eq.{event_type}"
            if severity:
                filters["severity"] = f"eq.{severity}"
            rows = await supabase_select(
                "client_events", filters, order_by="created_at.desc", limit=limit,
            )
        except Exception as e:
            return AgentResult(success=False, error=f"Client events query failed: {e}")

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.client_events",
            status="ok",
            inputs={"event_type": event_type, "severity": severity, "limit": limit},
            metadata={"count": len(rows)},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"events": rows, "count": len(rows), "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )
```

Wrapper:

```python
    async def admin_ops_client_events(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        event_type = params.get('event_type')
        severity = params.get('severity')
        limit = int(params.get('limit', 50))
        return await self._desk.get_client_events(ctx, event_type=event_type, severity=severity, limit=limit)
```

**Step 4: Run test — expect PASS**

**Step 5: Commit**

```bash
git commit -m "feat(admin): add client events tool to Ava Admin desk"
```

---

### Task 3: DB Performance Tool

**Files:**
- Modify: `ava_admin_desk.py`, `ava_admin.py`, `test_ava_admin.py`

**Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_get_db_performance() -> None:
    """Query DB performance RPCs via desk method."""
    mock_rpc = AsyncMock(side_effect=[
        {"hit_rate": 0.997},  # get_cache_hit_rate
        [{"query": "SELECT ...", "mean_exec_time": 12.5}],  # get_slow_queries
        [{"jobname": "receipt_archive", "schedule": "0 3 * * *"}],  # get_cron_jobs
    ])
    with patch("aspire_orchestrator.skillpacks.ava_admin_desk.supabase_rpc", mock_rpc):
        pack = AvaAdminSkillPack()
        ctx = _make_ctx()
        result = await pack.admin_ops_db_performance({}, ctx)
        assert result.success is True
        assert "cache_hit_rate" in result.data
        assert "slow_queries" in result.data
        assert "cron_jobs" in result.data
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

```python
    # =========================================================================
    # 17. DB Performance (GREEN — read-only Supabase RPCs)
    # =========================================================================

    async def get_db_performance(self, ctx: AgentContext) -> AgentResult:
        """Query DB performance metrics — same RPCs as /db-performance page."""
        data: dict[str, Any] = {}

        try:
            from aspire_orchestrator.services.supabase_client import supabase_rpc
        except Exception as e:
            return AgentResult(success=False, error=f"Supabase RPC import failed: {e}")

        # Cache hit rate
        try:
            result = await supabase_rpc("get_cache_hit_rate", {})
            data["cache_hit_rate"] = result
        except Exception as e:
            data["cache_hit_rate"] = {"error": str(e)[:100]}

        # Slow queries
        try:
            result = await supabase_rpc("get_slow_queries", {})
            data["slow_queries"] = result if isinstance(result, list) else []
        except Exception as e:
            data["slow_queries"] = {"error": str(e)[:100]}

        # Cron jobs
        try:
            result = await supabase_rpc("get_cron_jobs", {})
            data["cron_jobs"] = result if isinstance(result, list) else []
        except Exception as e:
            data["cron_jobs"] = {"error": str(e)[:100]}

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.db_performance",
            status="ok",
            inputs={},
            metadata={"sections_collected": len([v for v in data.values() if not isinstance(v, dict) or "error" not in v])},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={**data, "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )
```

Wrapper:

```python
    async def admin_ops_db_performance(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        return await self._desk.get_db_performance(ctx)
```

**Step 4: Run test — expect PASS**

**Step 5: Commit**

```bash
git commit -m "feat(admin): add DB performance tool to Ava Admin desk"
```

---

### Task 4: Trace Lookup Tool

**Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_get_trace() -> None:
    """Trace lookup by correlation_id — same data as /trace/:id page."""
    mock_receipts = [
        {"id": "r-1", "action_type": "invoice.create", "correlation_id": "corr-abc", "created_at": "2026-03-23T00:00:00Z"},
        {"id": "r-2", "action_type": "invoice.send", "correlation_id": "corr-abc", "created_at": "2026-03-23T00:01:00Z"},
    ]
    mock_calls = [
        {"id": "pc-1", "provider": "stripe", "correlation_id": "corr-abc", "created_at": "2026-03-23T00:00:30Z"},
    ]
    with patch("aspire_orchestrator.skillpacks.ava_admin_desk.query_receipts", return_value=mock_receipts), \
         patch("aspire_orchestrator.skillpacks.ava_admin_desk.supabase_select", new_callable=AsyncMock, return_value=mock_calls):
        pack = AvaAdminSkillPack()
        ctx = _make_ctx()
        result = await pack.admin_ops_trace({"correlation_id": "corr-abc"}, ctx)
        assert result.success is True
        assert len(result.data["receipts"]) == 2
        assert len(result.data["provider_calls"]) == 1
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

```python
    # =========================================================================
    # 18. Trace Lookup (GREEN — read-only cross-table query)
    # =========================================================================

    async def get_trace(
        self,
        ctx: AgentContext,
        *,
        correlation_id: str,
    ) -> AgentResult:
        """Full request trace by correlation_id — same data as /trace/:id page."""
        if not correlation_id or not correlation_id.strip():
            return AgentResult(success=False, error="Missing required parameter: correlation_id")

        # Get receipts for this correlation
        try:
            from aspire_orchestrator.services.receipt_store import query_receipts
            receipts = query_receipts(
                suite_id="system",
                correlation_id=correlation_id,
                limit=100,
            )
        except Exception as e:
            receipts = []
            logger.warning("Trace receipt query failed: %s", e)

        # Get provider calls for this correlation
        try:
            from aspire_orchestrator.services.supabase_client import supabase_select
            provider_calls = await supabase_select(
                "provider_call_log",
                {"correlation_id": f"eq.{correlation_id}"},
                order_by="created_at.asc",
                limit=100,
            )
        except Exception as e:
            provider_calls = []
            logger.warning("Trace provider call query failed: %s", e)

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.trace_lookup",
            status="ok",
            inputs={"correlation_id": correlation_id},
            metadata={"receipt_count": len(receipts), "provider_call_count": len(provider_calls)},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={
                "correlation_id": correlation_id,
                "receipts": receipts,
                "provider_calls": provider_calls,
                "total_events": len(receipts) + len(provider_calls),
                "voice_id": AVA_ADMIN_VOICE_ID,
            },
            receipt=receipt,
        )
```

Wrapper:

```python
    async def admin_ops_trace(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        correlation_id = str(params.get('correlation_id', '')).strip()
        if not correlation_id:
            return AgentResult(success=False, error='Missing required parameter: correlation_id')
        return await self._desk.get_trace(ctx, correlation_id=correlation_id)
```

**Step 4: Run test — expect PASS**

**Step 5: Commit**

```bash
git commit -m "feat(admin): add trace lookup tool to Ava Admin desk"
```

---

### Task 5: Incidents List + Outbox + N8n Ops + Webhook Health + Model Policy + Business Snapshot

These 6 methods follow the identical pattern. Add all at once to minimize file churn.

**Step 1: Write all 6 failing tests**

```python
@pytest.mark.asyncio
async def test_list_incidents() -> None:
    mock_store = MagicMock()
    mock_store.query_incidents.return_value = ([{"id": "inc-1", "state": "open"}], None)
    with patch("aspire_orchestrator.skillpacks.ava_admin_desk.get_admin_store", return_value=mock_store):
        pack = AvaAdminSkillPack()
        result = await pack.admin_ops_list_incidents({"state": "open", "limit": 20}, _make_ctx())
        assert result.success is True
        assert result.data["count"] == 1


@pytest.mark.asyncio
async def test_get_outbox_status() -> None:
    mock_rows = [{"id": "job-1", "status": "pending"}, {"id": "job-2", "status": "completed"}]
    with patch("aspire_orchestrator.skillpacks.ava_admin_desk.supabase_select", new_callable=AsyncMock, return_value=mock_rows):
        pack = AvaAdminSkillPack()
        result = await pack.admin_ops_outbox_status({"limit": 50}, _make_ctx())
        assert result.success is True
        assert result.data["count"] == 2


@pytest.mark.asyncio
async def test_get_n8n_operations() -> None:
    mock_rows = [
        {"action_type": "n8n.workflow.execute", "outcome": "success"},
        {"action_type": "n8n.workflow.execute", "outcome": "failure"},
    ]
    with patch("aspire_orchestrator.skillpacks.ava_admin_desk.supabase_select", new_callable=AsyncMock, return_value=mock_rows):
        pack = AvaAdminSkillPack()
        result = await pack.admin_ops_n8n_operations({"limit": 50}, _make_ctx())
        assert result.success is True
        assert result.data["count"] == 2


@pytest.mark.asyncio
async def test_get_webhook_health() -> None:
    mock_rows = [{"provider": "stripe", "event_type": "invoice.paid", "status": "delivered"}]
    with patch("aspire_orchestrator.skillpacks.ava_admin_desk.supabase_select", new_callable=AsyncMock, return_value=mock_rows):
        pack = AvaAdminSkillPack()
        result = await pack.admin_ops_webhook_health({"provider": "stripe"}, _make_ctx())
        assert result.success is True


@pytest.mark.asyncio
async def test_get_model_policy() -> None:
    pack = AvaAdminSkillPack()
    result = await pack.admin_ops_model_policy({}, _make_ctx())
    assert result.success is True
    assert "builder_model" in result.data


@pytest.mark.asyncio
async def test_get_business_snapshot() -> None:
    mock_events = [{"type": "revenue", "amount": 5000}, {"type": "expense", "amount": 2000}]
    mock_suites = [{"id": "s-1", "status": "active"}, {"id": "s-2", "status": "active"}]
    with patch("aspire_orchestrator.skillpacks.ava_admin_desk.supabase_select", new_callable=AsyncMock, side_effect=[mock_events, mock_suites]):
        pack = AvaAdminSkillPack()
        result = await pack.admin_ops_business_snapshot({"limit": 100}, _make_ctx())
        assert result.success is True
        assert "finance_events" in result.data
        assert "active_suites" in result.data
```

**Step 2: Run tests — expect 6 FAIL**

**Step 3: Implement all 6 desk methods + wrappers**

Add to `ava_admin_desk.py` (methods 19-24):

```python
    # =========================================================================
    # 19. List Incidents (GREEN — read-only)
    # =========================================================================

    async def list_incidents(
        self,
        ctx: AgentContext,
        *,
        state: str | None = None,
        severity: str | None = None,
        limit: int = 20,
    ) -> AgentResult:
        """List incidents — same data as /incidents page."""
        try:
            from aspire_orchestrator.services.admin_store import get_admin_store
            store = get_admin_store()
            incidents, cursor = store.query_incidents(state=state, severity=severity, limit=limit)
        except Exception as e:
            return AgentResult(success=False, error=f"Incidents query failed: {e}")

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.list_incidents",
            status="ok",
            inputs={"state": state, "severity": severity, "limit": limit},
            metadata={"count": len(incidents)},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"incidents": incidents, "count": len(incidents), "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )

    # =========================================================================
    # 20. Outbox Status (GREEN — read-only Supabase query)
    # =========================================================================

    async def get_outbox_status(
        self,
        ctx: AgentContext,
        *,
        limit: int = 50,
    ) -> AgentResult:
        """Query outbox_jobs table — same data as /outbox page."""
        try:
            from aspire_orchestrator.services.supabase_client import supabase_select
            rows = await supabase_select(
                "outbox_jobs", {}, order_by="created_at.desc", limit=limit,
            )
        except Exception as e:
            return AgentResult(success=False, error=f"Outbox status query failed: {e}")

        # Compute status counts
        counts: dict[str, int] = {}
        for row in rows:
            s = row.get("status", "unknown")
            counts[s] = counts.get(s, 0) + 1

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.outbox_status",
            status="ok",
            inputs={"limit": limit},
            metadata={"count": len(rows), "counts": counts},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"jobs": rows, "count": len(rows), "counts": counts, "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )

    # =========================================================================
    # 21. N8n Operations (GREEN — read-only receipt aggregation)
    # =========================================================================

    async def get_n8n_operations(
        self,
        ctx: AgentContext,
        *,
        limit: int = 50,
    ) -> AgentResult:
        """Query n8n workflow receipts — same data as /n8n-operations page."""
        try:
            from aspire_orchestrator.services.supabase_client import supabase_select
            rows = await supabase_select(
                "receipts",
                {"action_type": "like.n8n%"},
                order_by="created_at.desc",
                limit=limit,
            )
        except Exception as e:
            return AgentResult(success=False, error=f"N8n operations query failed: {e}")

        # Group by action_type
        by_type: dict[str, int] = {}
        for row in rows:
            at = row.get("action_type", "unknown")
            by_type[at] = by_type.get(at, 0) + 1

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.n8n_operations",
            status="ok",
            inputs={"limit": limit},
            metadata={"count": len(rows)},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"operations": rows, "count": len(rows), "by_type": by_type, "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )

    # =========================================================================
    # 22. Webhook Health (GREEN — read-only)
    # =========================================================================

    async def get_webhook_health(
        self,
        ctx: AgentContext,
        *,
        provider: str | None = None,
    ) -> AgentResult:
        """Query webhook delivery status."""
        try:
            from aspire_orchestrator.services.supabase_client import supabase_select
            filters: dict[str, Any] = {}
            if provider:
                filters["provider"] = f"eq.{provider}"
            rows = await supabase_select(
                "receipts",
                {**filters, "action_type": "like.webhook%"},
                order_by="created_at.desc",
                limit=50,
            )
        except Exception as e:
            return AgentResult(success=False, error=f"Webhook health query failed: {e}")

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.webhook_health",
            status="ok",
            inputs={"provider": provider},
            metadata={"count": len(rows)},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={"webhooks": rows, "count": len(rows), "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )

    # =========================================================================
    # 23. Model Policy (GREEN — read-only config)
    # =========================================================================

    async def get_model_policy(self, ctx: AgentContext) -> AgentResult:
        """Get current LLM builder model policy."""
        try:
            from aspire_orchestrator.services.secrets import get_secret
            builder_model = get_secret("ASPIRE_BUILDER_MODEL", "gpt-5-mini")
        except Exception:
            builder_model = "gpt-5-mini"

        policy = {
            "builder_model": builder_model,
            "brain_model": "gpt-5.2",
            "safety_model": "llama3:8b",
            "council_advisors": ["gpt-5.2", "gemini-3", "opus-4.6"],
        }

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.model_policy",
            status="ok",
            inputs={},
            metadata={"builder_model": builder_model},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={**policy, "voice_id": AVA_ADMIN_VOICE_ID},
            receipt=receipt,
        )

    # =========================================================================
    # 24. Business Snapshot (GREEN — read-only Supabase aggregation)
    # =========================================================================

    async def get_business_snapshot(
        self,
        ctx: AgentContext,
        *,
        limit: int = 100,
    ) -> AgentResult:
        """Aggregate business metrics — same data as /business/* pages."""
        try:
            from aspire_orchestrator.services.supabase_client import supabase_select
        except Exception as e:
            return AgentResult(success=False, error=f"Supabase import failed: {e}")

        # Finance events
        try:
            finance_events = await supabase_select(
                "finance_events", {}, order_by="created_at.desc", limit=limit,
            )
        except Exception:
            finance_events = []

        # Active suites
        try:
            suites = await supabase_select(
                "suite_profiles", {"status": "eq.active"}, limit=500,
            )
        except Exception:
            suites = []

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.business_snapshot",
            status="ok",
            inputs={"limit": limit},
            metadata={"finance_events": len(finance_events), "active_suites": len(suites)},
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={
                "finance_events": finance_events,
                "active_suites": len(suites),
                "suite_count": len(suites),
                "voice_id": AVA_ADMIN_VOICE_ID,
            },
            receipt=receipt,
        )
```

Add 6 wrappers to `ava_admin.py`:

```python
    async def admin_ops_list_incidents(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        state = params.get('state')
        severity = params.get('severity')
        limit = int(params.get('limit', 20))
        return await self._desk.list_incidents(ctx, state=state, severity=severity, limit=limit)

    async def admin_ops_outbox_status(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        limit = int(params.get('limit', 50))
        return await self._desk.get_outbox_status(ctx, limit=limit)

    async def admin_ops_n8n_operations(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        limit = int(params.get('limit', 50))
        return await self._desk.get_n8n_operations(ctx, limit=limit)

    async def admin_ops_webhook_health(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        provider = params.get('provider')
        return await self._desk.get_webhook_health(ctx, provider=provider)

    async def admin_ops_model_policy(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        return await self._desk.get_model_policy(ctx)

    async def admin_ops_business_snapshot(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        limit = int(params.get('limit', 100))
        return await self._desk.get_business_snapshot(ctx, limit=limit)
```

**Step 4: Run all tests — expect PASS**

Run: `cd backend/orchestrator && python -m pytest tests/test_ava_admin.py -v`

**Step 5: Commit**

```bash
git commit -m "feat(admin): add 6 data tools (incidents, outbox, n8n, webhooks, model policy, business snapshot)"
```

---

### Task 6: Update Ava Admin Persona — 24 Capabilities

**Files:**
- Modify: `backend/orchestrator/src/aspire_orchestrator/config/pack_personas/ava_admin_system_prompt.md`

**Step 1: Update capabilities list**

Replace the capabilities section with the full 24-method list:

```markdown
# Capabilities (24 methods)
Platform Health Pulse, Incident Triage, Robot Failure Triage, Provider Error Analysis, Council Dispatch, Learning Loop, Sentry Summary, Sentry Issues, Workflow Status, Approval Queue, Receipt Audit, Web Search, Council History, Metrics Snapshot, Provider Call Logs, Client Events, DB Performance, Trace Lookup, Incidents List, Outbox Status, N8n Operations, Webhook Health, Model Policy, Business Snapshot.
```

**Step 2: Commit**

```bash
git commit -m "docs(admin): update Ava Admin persona to list all 24 capabilities"
```

---

## Workstream 2: Council Production Wiring

### Task 7: Council Supabase Migration

**Files:**
- Create: `backend/supabase/migrations/20260323120000_council_sessions.sql`

**Step 1: Write the migration**

```sql
-- Council sessions and proposals — persistent Meeting of Minds state
-- Replaces in-memory _sessions dict in council_service.py

CREATE TABLE IF NOT EXISTS public.council_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id text NOT NULL,
    trigger text NOT NULL DEFAULT 'manual',
    evidence_pack jsonb NOT NULL DEFAULT '{}',
    members text[] NOT NULL DEFAULT ARRAY['gpt', 'gemini', 'claude'],
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'collecting', 'deliberating', 'decided', 'error')),
    decision jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    created_by text NOT NULL DEFAULT 'ava_admin'
);

CREATE TABLE IF NOT EXISTS public.council_proposals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid NOT NULL REFERENCES public.council_sessions(id) ON DELETE CASCADE,
    member text NOT NULL,
    root_cause text NOT NULL,
    fix_plan text NOT NULL,
    tests text[] NOT NULL DEFAULT '{}',
    risk_tier text NOT NULL DEFAULT 'green',
    evidence_links text[] NOT NULL DEFAULT '{}',
    confidence float NOT NULL DEFAULT 0.5 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status text NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted', 'accepted', 'rejected')),
    raw_response jsonb,
    model_used text,
    tokens_used integer DEFAULT 0,
    latency_ms integer DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_council_sessions_status ON public.council_sessions(status);
CREATE INDEX IF NOT EXISTS idx_council_sessions_incident ON public.council_sessions(incident_id);
CREATE INDEX IF NOT EXISTS idx_council_proposals_session ON public.council_proposals(session_id);

-- RLS: admin-only (service_role bypass)
ALTER TABLE public.council_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.council_proposals ENABLE ROW LEVEL SECURITY;

CREATE POLICY "council_sessions_service_role_all"
    ON public.council_sessions FOR ALL
    USING (auth.role() = 'service_role');

CREATE POLICY "council_proposals_service_role_all"
    ON public.council_proposals FOR ALL
    USING (auth.role() = 'service_role');

-- Admin read access
CREATE POLICY "council_sessions_admin_select"
    ON public.council_sessions FOR SELECT
    USING (
        auth.role() = 'authenticated'
        AND (auth.jwt() ->> 'email') IN (
            SELECT unnest(string_to_array(
                current_setting('app.admin_emails', true),
                ','
            ))
        )
    );

CREATE POLICY "council_proposals_admin_select"
    ON public.council_proposals FOR SELECT
    USING (
        auth.role() = 'authenticated'
        AND (auth.jwt() ->> 'email') IN (
            SELECT unnest(string_to_array(
                current_setting('app.admin_emails', true),
                ','
            ))
        )
    );
```

**Step 2: Apply migration**

Use Supabase MCP: `mcp__supabase__apply_migration` with name `council_sessions`

**Step 3: Commit**

```bash
git commit -m "feat(council): add council_sessions and council_proposals tables with RLS"
```

---

### Task 8: Multi-Model Advisor Service

**Files:**
- Create: `backend/orchestrator/src/aspire_orchestrator/services/council_advisors.py`
- Modify: `backend/orchestrator/tests/test_council_learning.py`

**Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_advisor_generates_proposal() -> None:
    """Each advisor model generates a structured proposal from evidence."""
    from aspire_orchestrator.services.council_advisors import query_advisor

    mock_response = {
        "root_cause": "Stripe webhook timeout causing invoice status desync",
        "fix_plan": "Add idempotency key to webhook handler, increase timeout to 30s",
        "tests": ["test_webhook_idempotency", "test_timeout_recovery"],
        "risk_tier": "yellow",
        "confidence": 0.85,
    }

    with patch("aspire_orchestrator.services.council_advisors._call_openai", new_callable=AsyncMock, return_value=mock_response):
        result = await query_advisor(
            advisor="gpt",
            evidence_pack={"incident_id": "inc-1", "error": "webhook timeout"},
            incident_id="inc-1",
        )
        assert result["root_cause"] == mock_response["root_cause"]
        assert result["confidence"] == 0.85
        assert result["advisor"] == "gpt"
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

```python
"""Council Advisors — Multi-model API calls for Meeting of Minds.

Each advisor receives a read-only evidence pack and returns a structured
triage proposal. Advisors NEVER execute — they only analyze (Law #7).

Models:
  - GPT-5.2 (OpenAI): Architecture critic, root cause analysis
  - Gemini 3 (Google): Research cross-check, alternative approaches
  - Opus 4.6 (Anthropic): Implementation planning

Budget: Configurable per-session, default $5 for testing.
Timeout: 30s per advisor call.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_ADVISOR_SYSTEM_PROMPT = """You are a council advisor in the Aspire platform's Meeting of Minds.
You receive an evidence pack about a platform incident and must produce a structured triage proposal.

You are READ-ONLY. You cannot execute any actions. You analyze and propose.

Respond with ONLY a JSON object (no markdown, no explanation) with these fields:
{
  "root_cause": "Your analysis of the root cause",
  "fix_plan": "Step-by-step fix plan",
  "tests": ["test_names to validate the fix"],
  "risk_tier": "green|yellow|red",
  "confidence": 0.0-1.0,
  "reasoning": "Why you believe this diagnosis"
}"""

_MODEL_MAP = {
    "gpt": {"provider": "openai", "model": "gpt-5.2", "role": "Architecture critic, root cause analysis"},
    "gemini": {"provider": "google", "model": "gemini-3", "role": "Research cross-check, alternative approaches"},
    "claude": {"provider": "anthropic", "model": "claude-opus-4-6", "role": "Implementation planning"},
}

_TIMEOUT_SECONDS = 30


async def _call_openai(prompt: str, model: str = "gpt-5.2") -> dict[str, Any]:
    """Call OpenAI API for GPT advisor."""
    import httpx
    from aspire_orchestrator.services.secrets import get_secret

    api_key = get_secret("ASPIRE_OPENAI_API_KEY") or get_secret("ASPIRE_OPENAI_KEY")
    if not api_key:
        raise ValueError("OpenAI API key not configured")

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _ADVISOR_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 1000,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)


async def _call_google(prompt: str, model: str = "gemini-3") -> dict[str, Any]:
    """Call Google Generative AI API for Gemini advisor."""
    import httpx
    from aspire_orchestrator.services.secrets import get_secret

    api_key = get_secret("ASPIRE_GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Google API key not configured")

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": f"{_ADVISOR_SYSTEM_PROMPT}\n\n{prompt}"}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1000},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(text)


async def _call_anthropic(prompt: str, model: str = "claude-opus-4-6") -> dict[str, Any]:
    """Call Anthropic API for Claude advisor."""
    import httpx
    from aspire_orchestrator.services.secrets import get_secret

    api_key = get_secret("ASPIRE_ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("Anthropic API key not configured")

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1000,
                "system": _ADVISOR_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"]
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(text)


_PROVIDER_MAP = {
    "openai": _call_openai,
    "google": _call_google,
    "anthropic": _call_anthropic,
}


async def query_advisor(
    *,
    advisor: str,
    evidence_pack: dict[str, Any],
    incident_id: str,
) -> dict[str, Any]:
    """Query a single council advisor and return structured proposal.

    Returns dict with: advisor, root_cause, fix_plan, tests, risk_tier,
    confidence, reasoning, model_used, tokens_used, latency_ms.
    """
    config = _MODEL_MAP.get(advisor)
    if not config:
        raise ValueError(f"Unknown advisor: {advisor}. Valid: {list(_MODEL_MAP.keys())}")

    prompt = (
        f"Incident ID: {incident_id}\n"
        f"Your role: {config['role']}\n\n"
        f"Evidence pack:\n{json.dumps(evidence_pack, indent=2, default=str)}\n\n"
        "Analyze this incident and produce your triage proposal as JSON."
    )

    provider_fn = _PROVIDER_MAP[config["provider"]]
    start = time.monotonic()

    try:
        result = await provider_fn(prompt, config["model"])
    except Exception as e:
        logger.error("Council advisor %s failed: %s", advisor, e)
        return {
            "advisor": advisor,
            "model_used": config["model"],
            "root_cause": f"Advisor {advisor} failed to respond: {e}",
            "fix_plan": "",
            "tests": [],
            "risk_tier": "yellow",
            "confidence": 0.0,
            "reasoning": f"Error: {e}",
            "tokens_used": 0,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "error": str(e),
        }

    latency_ms = int((time.monotonic() - start) * 1000)

    return {
        "advisor": advisor,
        "model_used": config["model"],
        "root_cause": result.get("root_cause", ""),
        "fix_plan": result.get("fix_plan", ""),
        "tests": result.get("tests", []),
        "risk_tier": result.get("risk_tier", "yellow"),
        "confidence": float(result.get("confidence", 0.5)),
        "reasoning": result.get("reasoning", ""),
        "tokens_used": 0,  # TODO: extract from response headers
        "latency_ms": latency_ms,
    }
```

**Step 4: Run test — expect PASS**

**Step 5: Commit**

```bash
git commit -m "feat(council): add multi-model advisor service with OpenAI, Google, Anthropic"
```

---

### Task 9: Rewrite Council Service for Supabase Persistence + Real Advisors

**Files:**
- Modify: `backend/orchestrator/src/aspire_orchestrator/services/council_service.py`
- Modify: `backend/orchestrator/tests/test_council_learning.py`

**Step 1: Write tests for persistent council flow**

```python
@pytest.mark.asyncio
async def test_council_spawn_persists_to_supabase() -> None:
    """Council session is saved to Supabase, not just in-memory."""
    from aspire_orchestrator.services.council_service import spawn_council
    mock_insert = AsyncMock(return_value=[{"id": "session-uuid"}])
    with patch("aspire_orchestrator.services.council_service.supabase_insert", mock_insert):
        session, receipt = await spawn_council(incident_id="inc-test")
        assert session.session_id is not None
        mock_insert.assert_called_once()
        assert receipt["action_type"] == "council.session.created"


@pytest.mark.asyncio
async def test_council_runs_real_advisors() -> None:
    """Council dispatches to all 3 advisors and collects proposals."""
    from aspire_orchestrator.services.council_service import run_council

    mock_advisor = AsyncMock(return_value={
        "advisor": "gpt", "root_cause": "timeout", "fix_plan": "increase timeout",
        "tests": [], "risk_tier": "green", "confidence": 0.8, "reasoning": "clear evidence",
        "model_used": "gpt-5.2", "tokens_used": 0, "latency_ms": 500,
    })
    mock_insert = AsyncMock(return_value=[{"id": "p-uuid"}])
    mock_session_insert = AsyncMock(return_value=[{"id": "s-uuid"}])

    with patch("aspire_orchestrator.services.council_service.query_advisor", mock_advisor), \
         patch("aspire_orchestrator.services.council_service.supabase_insert", mock_session_insert), \
         patch("aspire_orchestrator.services.council_service._insert_proposal", mock_insert):
        result = await run_council(incident_id="inc-test", evidence_pack={"error": "timeout"})
        assert result["status"] == "decided"
        assert len(result["proposals"]) == 3
        assert result["decision"]["selected_member"] is not None
```

**Step 2: Run tests — expect FAIL**

**Step 3: Rewrite `council_service.py`**

Replace the entire file with the persistent version that:
1. Uses `supabase_insert` / `supabase_select` / `supabase_update` for all state
2. Calls `query_advisor()` from `council_advisors.py` for each model
3. Runs all 3 advisors concurrently with `asyncio.gather()`
4. LLM-powered adjudication: uses GPT-5.2 to reason across proposals
5. Still emits receipts for every step (Law #2)

Key changes:
- `spawn_council()` → async, inserts to `council_sessions` table
- `submit_proposal()` → async, inserts to `council_proposals` table
- NEW `run_council()` → orchestrates full flow: spawn → 3x advisor → adjudicate
- `adjudicate()` → async, uses LLM to pick winner with reasoning
- Remove `_sessions` in-memory dict
- `get_session()` / `list_sessions()` → query Supabase

**Step 4: Run tests — expect PASS**

**Step 5: Commit**

```bash
git commit -m "feat(council): rewrite council_service for Supabase persistence + real multi-model advisors"
```

---

### Task 10: Update Council Dispatch in Admin Desk

**Files:**
- Modify: `backend/orchestrator/src/aspire_orchestrator/skillpacks/ava_admin_desk.py` (dispatch_council method)

**Step 1: Update dispatch_council to call `run_council()` instead of A2A dispatch**

The current dispatch sends to an A2A agent that doesn't exist. Replace with direct `run_council()` call that runs all 3 advisors and adjudicates.

```python
    async def dispatch_council(
        self,
        ctx: AgentContext,
        *,
        incident_id: str,
        evidence_pack: dict[str, Any],
    ) -> AgentResult:
        """Spawn Meeting of Minds council — runs all 3 advisors + adjudicates."""
        try:
            from aspire_orchestrator.services.council_service import run_council
            result = await run_council(
                incident_id=incident_id,
                evidence_pack=evidence_pack,
                suite_id=ctx.suite_id,
                office_id=ctx.office_id,
            )
        except Exception as e:
            logger.error("Council dispatch failed: %s", e)
            return AgentResult(success=False, error=f"Council dispatch failed: {e}")

        receipt = self.build_receipt(
            ctx=ctx,
            event_type="admin.council_dispatched",
            status="ok",
            inputs={"incident_id": incident_id, "council_session_id": result["session_id"]},
            metadata={
                "advisors": ["gpt-5.2", "gemini-3", "opus-4.6"],
                "status": result["status"],
                "selected_member": result.get("decision", {}).get("selected_member"),
            },
        )
        await self.emit_receipt(receipt)

        return AgentResult(
            success=True,
            data={
                "council_session_id": result["session_id"],
                "incident_id": incident_id,
                "status": result["status"],
                "proposals": result.get("proposals", []),
                "decision": result.get("decision"),
                "advisors": ["gpt-5.2", "gemini-3", "opus-4.6"],
                "voice_id": AVA_ADMIN_VOICE_ID,
            },
            receipt=receipt,
        )
```

**Step 2: Run existing council tests + new tests**

Run: `cd backend/orchestrator && python -m pytest tests/test_council_learning.py tests/test_ava_admin.py -v`

**Step 3: Commit**

```bash
git commit -m "feat(council): wire dispatch_council to run_council with real advisors"
```

---

### Task 11: Full Test Suite + Final Commit

**Step 1: Run full backend test suite**

```bash
cd backend/orchestrator && python -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

Expected: All new tests pass, zero new failures.

**Step 2: Verify council endpoint still works**

The GET `/admin/ops/council/{session_id}` route should now query Supabase instead of in-memory state. Verify:

```bash
cd backend/orchestrator && python -m pytest tests/test_wave6_admin_ava.py -v -k council
```

**Step 3: Final commit if any fixes needed**

```bash
git commit -m "test(admin): verify full test suite passes with 24 tools + council production wiring"
```

---

## Files Changed Summary

| File | Action | Description |
|------|--------|-------------|
| `skillpacks/ava_admin_desk.py` | MODIFY | Add 10 new desk methods (15-24) + rewrite dispatch_council |
| `skillpacks/ava_admin.py` | MODIFY | Add 10 new wrappers |
| `services/council_service.py` | REWRITE | Supabase persistence, real advisors, async |
| `services/council_advisors.py` | CREATE | Multi-model API calls (OpenAI, Google, Anthropic) |
| `config/pack_personas/ava_admin_system_prompt.md` | MODIFY | Update capabilities to 24 methods |
| `tests/test_ava_admin.py` | MODIFY | Add 10+ new tests |
| `tests/test_council_learning.py` | MODIFY | Add persistence + advisor tests |
| `supabase/migrations/20260323120000_council_sessions.sql` | CREATE | council_sessions + council_proposals tables |

---

## Verification Checklist

- [ ] All 10 new desk methods return `AgentResult` with `voice_id`
- [ ] All 10 new methods emit receipts (Law #2)
- [ ] All methods are GREEN tier (read-only)
- [ ] Council sessions persist to Supabase (not in-memory)
- [ ] Council advisors make real API calls with 30s timeout
- [ ] Council adjudication uses LLM reasoning
- [ ] RLS on council tables: service_role + admin-only
- [ ] Ava Admin persona lists all 24 capabilities
- [ ] Zero new test failures
