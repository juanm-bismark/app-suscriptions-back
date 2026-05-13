-- ============================================================
-- Schema inicial — PostgreSQL (sin Supabase)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1. Enum de roles
CREATE TYPE app_role AS ENUM ('public', 'admin', 'manager', 'member');

-- 2. Tabla de usuarios (reemplaza auth.users de Supabase)
CREATE TABLE users (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    email            VARCHAR     NOT NULL UNIQUE,
    hashed_password  VARCHAR     NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 3. Tabla de empresas
CREATE TABLE companies (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name       VARCHAR     NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 4. Configuración por empresa
CREATE TABLE company_settings (
    company_id UUID        PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    settings   JSONB       NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 4b. Credenciales cifradas por empresa/proveedor
CREATE TABLE company_provider_credentials (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id       UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    provider         TEXT        NOT NULL,
    credentials_enc  TEXT        NOT NULL,
    account_scope    JSONB       NOT NULL DEFAULT '{}',
    active           BOOLEAN     NOT NULL DEFAULT TRUE,
    rotated_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX company_provider_credentials_active_idx
    ON company_provider_credentials (company_id, provider)
    WHERE active = TRUE;

-- 4c. Vínculo puntual entre empresa local y compañía/cuenta del proveedor
CREATE TABLE company_provider_mappings (
    company_id             UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    provider               TEXT        NOT NULL,
    provider_company_code  TEXT        NOT NULL,
    provider_company_name  TEXT,
    clie_id                INTEGER,
    settings               JSONB       NOT NULL DEFAULT '{}',
    active                 BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, provider)
);

CREATE INDEX company_provider_mappings_provider_code_idx
    ON company_provider_mappings (provider, provider_company_code);

-- 5. Perfiles de usuario
CREATE TABLE profiles (
    id         UUID        PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    company_id UUID        REFERENCES companies(id),
    role       app_role    NOT NULL DEFAULT 'member',
    full_name  VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 6. Refresh tokens (opaque, rotación en cada uso)
CREATE TABLE refresh_tokens (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token      VARCHAR     NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
