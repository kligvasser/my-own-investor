-- 008: track market expiry so the dashboard can flag date-dependent markets
-- (e.g. "largest company by July 31") that need their slug rotated.
ALTER TABLE polymarket_markets ADD COLUMN IF NOT EXISTS end_date TIMESTAMP;
