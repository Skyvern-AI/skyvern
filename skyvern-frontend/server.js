// server.js  –  run with:  node server.js
import "dotenv/config"; // same as require('dotenv').config()
import express from "express";
import crypto from "crypto";

const app = express();

// we need raw bytes so the signature matches exactly
app.use(express.raw({ type: "*/*" }));

/**
 * Equivalent to your generate_skyvern_signature() helper.
 * This assumes an HMAC‑SHA256, which is what most webhook
 * schemes use.  If your Python helper differs, adjust here.
 */
function generateSkyvernSignature(payloadBuf, apiKey) {
  const realApiKey = process.env.SKYVERN_API_KEY;
  return crypto
    .createHmac("sha256", realApiKey) // use the API key as the secret
    .update(payloadBuf) // already a Buffer
    .digest("hex");
}

function handler(req, res) {
  const signature = req.header("x-skyvern-signature");
  const timestamp = req.header("x-skyvern-timestamp");
  const payload = req.body; // Buffer

  if (!signature || !timestamp) {
    console.error("Webhook signature or timestamp missing", {
      signature,
      timestamp,
      payload: payload.toString(),
    });
    return res.status(400).send("Missing webhook signature or timestamp");
  }

  const generatedSignature = generateSkyvernSignature(
    payload,
    process.env.SKYVERN_API_KEY || "unset",
  );

  console.info("Webhook received", {
    signature,
    timestamp,
    generatedSignature,
    apiKey: process.env.SKYVERN_API_KEY,
    validSignature: signature === generatedSignature,
  });

  return res.status(200).send("webhook validation");
}

// Support both /webhook and /webhook/
app.post("/webhook", handler);
app.post("/webhook/", handler);

app.listen(8008, () =>
  console.log("✅  Listening on http://localhost:8008/webhook"),
);
