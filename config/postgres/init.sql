-- One Postgres, separate databases per engine.
CREATE DATABASE hindsight;
CREATE DATABASE lightrag;
CREATE DATABASE sourcebot;
\connect hindsight
CREATE EXTENSION IF NOT EXISTS vector;
\connect lightrag
CREATE EXTENSION IF NOT EXISTS vector;
