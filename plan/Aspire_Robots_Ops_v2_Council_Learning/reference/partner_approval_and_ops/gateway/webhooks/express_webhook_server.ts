import express from "express";
import crypto from "crypto";

/**
 * Scaffold: provider-agnostic webhook receiver.
 * Replace signature logic with provider-specific schemes.
 * Use raw body bytes for verification.
 */

const app = express();
app.use(express.raw({ type: "*/*", limit: "2mb" }));

function timingSafeEqual(a: Buffer, b: Buffer) {
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

app.post("/webhooks/:provider", async (req, res) => {
  const provider = req.params.provider;
  const rawBody = req.body as Buffer;

  const sigHeader = String(req.headers["x-signature"] || "");
  const timestamp = String(req.headers["x-timestamp"] || "");

  const secret = process.env.WEBHOOK_SECRET || "";
  const base = `${timestamp}.${rawBody.toString("utf8")}`;
  const computed = crypto.createHmac("sha256", secret).update(base).digest("hex");

  if (!sigHeader || !timingSafeEqual(Buffer.from(sigHeader), Buffer.from(computed))) {
    // TODO: emit receipt/event: WEBHOOK_SIGNATURE_INVALID (redacted)
    return res.status(401).send("invalid signature");
  }

  // TODO: extract event_id; dedupe on (provider, event_id)
  // TODO: emit receipt/event: webhook.received
  // TODO: call Trust Spine RPC to ingest webhook + link trace_id

  res.status(200).send("ok");
});

app.listen(process.env.PORT || 3001, () => console.log("Webhook gateway running"));
