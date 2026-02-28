import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

export type AuthedContext = {
  supabase: ReturnType<typeof createClient>;
  userId: string;
  user: any;
};

function env(name: string): string {
  const v = Deno.env.get(name);
  if (!v) throw new Error(`Missing env var: ${name}`);
  return v;
}

/**
 * Creates an authenticated Supabase client from the incoming request's Authorization header.
 * Uses the ANON key and relies on RLS for access control.
 */
export async function requireUser(req: Request): Promise<AuthedContext> {
  const url = env("SUPABASE_URL");
  const anon = env("SUPABASE_ANON_KEY");

  const authHeader = req.headers.get("Authorization");
  if (!authHeader) throw new Error("Missing Authorization header");

  const supabase = createClient(url, anon, {
    global: {
      headers: { Authorization: authHeader },
    },
  });

  const userRes = await supabase.auth.getUser();
  const user = userRes.data.user;
  if (!user) throw new Error("Unauthenticated");

  return { supabase, userId: user.id, user };
}

/**
 * Strict check: requires the caller to be a member of the provided tenant.
 */
export async function requireTenantMember(req: Request, tenantId: string): Promise<AuthedContext> {
  const { supabase, userId, user } = await requireUser(req);

  const { data, error } = await supabase
    .from("tenant_memberships")
    .select("tenant_id")
    .eq("tenant_id", tenantId)
    .eq("user_id", userId)
    .maybeSingle();

  if (error) throw new Error(error.message);
  if (!data) throw new Error("Not a tenant member");

  return { supabase, userId, user };
}

/**
 * Canonical check: requires caller to be a member of the provided suite_id.
 * Internally maps suite_id -> tenant_id via app.suites (legacy membership table).
 */
export async function requireSuiteMember(req: Request, suiteId: string): Promise<AuthedContext & { tenantId: string }> {
  const { supabase, userId, user } = await requireUser(req);

  // Resolve tenant_id from app.suites
  const { data: suiteRow, error: suiteErr } = await supabase
    .schema('app')
    .from('suites')
    .select('tenant_id')
    .eq('suite_id', suiteId)
    .maybeSingle();

  if (suiteErr) throw new Error(suiteErr.message);
  const tenantId = (suiteRow as any)?.tenant_id as string | undefined;
  if (!tenantId) throw new Error('Unknown suite_id');

  // Membership still keyed by tenant_id for compatibility
  const { data, error } = await supabase
    .from('tenant_memberships')
    .select('tenant_id')
    .eq('tenant_id', tenantId)
    .eq('user_id', userId)
    .maybeSingle();

  if (error) throw new Error(error.message);
  if (!data) throw new Error('Not a suite member');

  return { supabase, userId, user, tenantId };
}
