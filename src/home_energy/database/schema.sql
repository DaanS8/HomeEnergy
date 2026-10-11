-- The repository supplies the fixed project schema as a quoted SQL identifier.
CREATE SCHEMA IF NOT EXISTS {schema};
SET LOCAL search_path TO {schema};

CREATE TABLE IF NOT EXISTS energy_balance_days (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plant_id text NOT NULL CHECK (plant_id <> ''),
    selected_date date NOT NULL,
    timezone text NOT NULL,
    fetched_at timestamptz NOT NULL,
    sample_count integer NOT NULL CHECK (sample_count > 0),
    available_series text[] NOT NULL CHECK (cardinality(available_series) > 0),
    empty_series text[] NOT NULL,
    metadata jsonb NOT NULL,
    UNIQUE (plant_id, selected_date)
);

CREATE TABLE IF NOT EXISTS measurement_samples (
    day_id bigint NOT NULL REFERENCES energy_balance_days(id),
    sample_index integer NOT NULL CHECK (sample_index >= 0),
    axis_label text NOT NULL CHECK (axis_label <> ''),
    observed_at timestamptz,
    measurement text NOT NULL,
    value numeric,
    original_value text,
    unit text,
    PRIMARY KEY (day_id, sample_index, measurement)
);

CREATE TABLE IF NOT EXISTS daily_metrics (
    day_id bigint NOT NULL REFERENCES energy_balance_days(id),
    name text NOT NULL,
    value numeric,
    original_value text,
    unit text,
    PRIMARY KEY (day_id, name)
);

CREATE TABLE IF NOT EXISTS day_ahead_price_days (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    market text NOT NULL CHECK (market = 'BE'),
    selected_date date NOT NULL,
    timezone text NOT NULL CHECK (timezone = 'Europe/Brussels'),
    fetched_at timestamptz NOT NULL,
    UNIQUE (market, selected_date)
);

CREATE TABLE IF NOT EXISTS price_intervals (
    day_id bigint NOT NULL REFERENCES day_ahead_price_days(id),
    start_at timestamptz NOT NULL,
    end_at timestamptz NOT NULL CHECK (end_at > start_at),
    price_eur_per_mwh numeric NOT NULL
        CHECK (price_eur_per_mwh NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)),
    PRIMARY KEY (day_id, start_at)
);
