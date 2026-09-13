-- One Postgres (unit `postgres`, user `cerebro`), one database per engine. Mounted by the provisioner into
-- /docker-entrypoint-initdb.d/, so it runs once, on the first start with an empty data volume.
CREATE DATABASE lightrag;
CREATE DATABASE hindsight;
CREATE DATABASE keycloak;
CREATE DATABASE cerebro;
\connect lightrag
CREATE EXTENSION IF NOT EXISTS vector;
\connect cerebro
CREATE EXTENSION IF NOT EXISTS vector;
