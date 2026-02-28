import { requireSuiteMember, requireTenantMember } from "../_shared/auth.ts";

Deno.serve(async (req) => {
  try {
    const url = new URL(req.url);

    if (req.method === "GET") {
      // Canonical: suite_id (UUID). Legacy: tenant_id.
      let suiteId = url.searchParams.get("suite_id");
      const legacyTenantId = url.searchParams.get("tenant_id");

      if (!suiteId && legacyTenantId) {
        // Backwards compat: resolve suite_id from tenant_id
        const { supabase } = await requireTenantMember(req, legacyTenantId);
        const { data, error } = await supabase
          .schema("app")
          .from("suites")
          .select("suite_id")
          .eq("tenant_id", legacyTenantId)
          .maybeSingle();
        if (error) return Response.json({ error: error.message }, { status: 500 });
        suiteId = (data as any)?.suite_id ?? null;
      }

      if (!suiteId) return new Response("suite_id required", { status: 400 });

      const { supabase } = await requireSuiteMember(req, suiteId);
      const status = url.searchParams.get("status");
      const limit = Number(url.searchParams.get("limit") || "50");

      let q = supabase.from("inbox_items")
        .select("*")
        .eq("suite_id", suiteId)
        .order("updated_at", { ascending: false })
        .limit(limit);

      if (status) q = q.eq("status", status);

      const { data, error } = await q;
      if (error) return Response.json({ error: error.message }, { status: 500 });
      return Response.json({ items: data });
    }

    if (req.method === "POST") {
      const body = await req.json();
      const suiteId = body.suite_id as string | undefined;
      const legacyTenantId = body.tenant_id as string | undefined;

      if (!suiteId && !legacyTenantId) return new Response("suite_id required", { status: 400 });

      let suiteIdResolved = suiteId;

      if (!suiteIdResolved && legacyTenantId) {
        // Backwards compat: resolve suite_id from tenant_id
        const { supabase } = await requireTenantMember(req, legacyTenantId);
        const { data, error } = await supabase
          .schema("app")
          .from("suites")
          .select("suite_id")
          .eq("tenant_id", legacyTenantId)
          .maybeSingle();
        if (error) return Response.json({ error: error.message }, { status: 500 });
        suiteIdResolved = (data as any)?.suite_id ?? null;
      }

      if (!suiteIdResolved) return new Response("suite_id required", { status: 400 });

      const { supabase, userId } = await requireSuiteMember(req, suiteIdResolved);

      const payload = {
        id: crypto.randomUUID(),
        suite_id: suiteIdResolved,
        // tenant_id is set by DB trigger from suite_id
        office_id: body.office_id ?? null,
        type: body.type,
        title: body.title,
        preview: body.preview ?? null,
        priority: body.priority,
        status: "NEW",
        assigned_to: null,
        unread: true,
        metadata: body.metadata ?? {},
      };

      const { data, error } = await supabase.from("inbox_items").insert(payload).select("*").single();
      if (error) return Response.json({ error: error.message }, { status: 500 });

      void userId;
      return Response.json(data, { status: 201 });
    }

    return new Response("Method not allowed", { status: 405 });
  } catch (e) {
    return Response.json({ error: String((e as any)?.message ?? e) }, { status: 500 });
  }
});
