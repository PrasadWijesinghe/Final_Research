-- Runs once, when the data volume is first created (docker-entrypoint-initdb.d).
-- Idempotent so it is also safe to run by hand.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS platform;
CREATE SCHEMA IF NOT EXISTS dependency;
CREATE SCHEMA IF NOT EXISTS build;
CREATE SCHEMA IF NOT EXISTS testing;
CREATE SCHEMA IF NOT EXISTS infra;
