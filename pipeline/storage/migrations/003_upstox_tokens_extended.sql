-- Migration 003: add dedicated extended_token column to upstox_tokens
-- extended_token is Upstox's long-lived (~1 year) token returned during OAuth.
-- Stored separately so the expiry check job can monitor it independently.

ALTER TABLE upstox_tokens ADD COLUMN IF NOT EXISTS extended_token TEXT;
