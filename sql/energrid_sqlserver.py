"""EnerGrid - Creacion de modelo relacional (PK/FK/indices) y carga a SQL Server."""
import re
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Modelo relacional (orden = padres -> hijos)
# ---------------------------------------------------------------------------
TABLES = {
    "dim_zona": "[zone_id] INT NOT NULL, [zone_name] NVARCHAR(50) NOT NULL, [region] NVARCHAR(20) NOT NULL",
    "dim_circuito": "[circuit_id] INT NOT NULL, [circuit_name] NVARCHAR(30) NOT NULL, [zone_id] INT NOT NULL, "
                    "[voltage_level] NVARCHAR(20) NOT NULL",
    "dim_transformador": "[transformer_id] INT NOT NULL, [transformer_name] NVARCHAR(30) NOT NULL, "
                         "[circuit_id] INT NOT NULL, [zone_id] INT NOT NULL, [capacity_kva] INT NOT NULL, "
                         "[latitude] DECIMAL(9,6) NULL, [longitude] DECIMAL(9,6) NULL",
    "dim_tarifa": "[tariff_id] INT NOT NULL, [tariff_name] NVARCHAR(30) NOT NULL, "
                  "[rate_per_kwh] DECIMAL(8,4) NOT NULL",
    "dim_causa": "[cause_id] INT NOT NULL, [cause_name] NVARCHAR(50) NOT NULL, [planned] BIT NOT NULL",
    "dim_tiempo": "[date_key] INT NOT NULL, [date] DATE NOT NULL, [year] INT NOT NULL, [quarter] INT NOT NULL, "
                  "[month] INT NOT NULL, [month_name] NVARCHAR(20) NOT NULL, [day] INT NOT NULL, "
                  "[day_of_week] INT NOT NULL, [is_weekend] BIT NOT NULL",
    "dim_cliente": "[client_id] INT NOT NULL, [client_code] NVARCHAR(20) NOT NULL, "
                   "[client_type] NVARCHAR(20) NOT NULL, [tariff_id] INT NOT NULL, [zone_id] INT NOT NULL, "
                   "[circuit_id] INT NOT NULL, [transformer_id] INT NOT NULL, [latitude] DECIMAL(9,6) NULL, "
                   "[longitude] DECIMAL(9,6) NULL, [active] BIT NULL, [connection_date] DATE NULL",
    "dim_medidor": "[meter_id] INT NOT NULL, [meter_serial] NVARCHAR(20) NOT NULL, [client_id] INT NOT NULL, "
                   "[meter_type] NVARCHAR(20) NOT NULL, [ami_enabled] BIT NULL, [installed_date] DATE NULL, "
                   "[status] NVARCHAR(20) NOT NULL",
    "fact_lectura_energia": "[reading_id] INT NOT NULL, [client_id] INT NOT NULL, [meter_id] INT NOT NULL, "
                            "[reading_date] DATE NOT NULL, [date_key] INT NOT NULL, [period_year] INT NOT NULL, "
                            "[period_month] INT NOT NULL, [registered_kwh] DECIMAL(14,3) NULL, "
                            "[estimated_kwh] DECIMAL(14,3) NULL, [consumption_kwh] DECIMAL(14,3) NULL, "
                            "[reading_status] NVARCHAR(20) NOT NULL, [ami_valid] BIT NULL",
    "fact_facturacion": "[invoice_id] INT NOT NULL, [client_id] INT NOT NULL, [reading_id] INT NOT NULL, "
                        "[invoice_date] DATE NOT NULL, [due_date] DATE NOT NULL, [period_year] INT NOT NULL, "
                        "[period_month] INT NOT NULL, [billed_kwh] DECIMAL(14,3) NULL, [is_estimated] BIT NULL, "
                        "[amount_due] DECIMAL(14,2) NOT NULL",
    "fact_pago": "[payment_id] INT NOT NULL, [invoice_id] INT NOT NULL, [client_id] INT NOT NULL, "
                 "[payment_date] DATE NOT NULL, [payment_amount] DECIMAL(14,2) NOT NULL, "
                 "[payment_method] NVARCHAR(20) NULL, [is_date_imputed] BIT NULL",
    "fact_energia_transformador_diaria": "[energy_record_id] INT NOT NULL, [date_key] INT NOT NULL, "
                                         "[date] DATE NOT NULL, [transformer_id] INT NOT NULL, "
                                         "[injected_kwh] DECIMAL(16,3) NOT NULL, "
                                         "[delivered_kwh] DECIMAL(16,3) NOT NULL, [is_imputed] BIT NULL",
    "fact_interrupcion": "[outage_id] INT NOT NULL, [start_datetime] DATETIME2(0) NOT NULL, "
                         "[end_datetime] DATETIME2(0) NOT NULL, [date_key] INT NOT NULL, [circuit_id] INT NOT NULL, "
                         "[transformer_id] INT NOT NULL, [zone_id] INT NOT NULL, [cause_id] INT NOT NULL, "
                         "[duration_minutes] INT NULL, [customers_affected] INT NULL, [planned] BIT NULL, "
                         "[latitude] DECIMAL(9,6) NULL, [longitude] DECIMAL(9,6) NULL",
}

PK = {"dim_zona": "zone_id", "dim_circuito": "circuit_id", "dim_transformador": "transformer_id",
      "dim_tarifa": "tariff_id", "dim_causa": "cause_id", "dim_tiempo": "date_key", "dim_cliente": "client_id",
      "dim_medidor": "meter_id", "fact_lectura_energia": "reading_id", "fact_facturacion": "invoice_id",
      "fact_pago": "payment_id", "fact_energia_transformador_diaria": "energy_record_id",
      "fact_interrupcion": "outage_id"}

UNIQUE = {"fact_lectura_energia": ["client_id", "period_year", "period_month"],
          "fact_facturacion": ["client_id", "period_year", "period_month"],
          "fact_energia_transformador_diaria": ["transformer_id", "date_key"]}

# (tabla_hija, columna, tabla_padre)  -> la columna padre es la PK del padre
FKS = [
    ("dim_circuito", "zone_id", "dim_zona"),
    ("dim_transformador", "circuit_id", "dim_circuito"),
    ("dim_transformador", "zone_id", "dim_zona"),
    ("dim_cliente", "tariff_id", "dim_tarifa"),
    ("dim_cliente", "zone_id", "dim_zona"),
    ("dim_cliente", "circuit_id", "dim_circuito"),
    ("dim_cliente", "transformer_id", "dim_transformador"),
    ("dim_medidor", "client_id", "dim_cliente"),
    ("fact_lectura_energia", "client_id", "dim_cliente"),
    ("fact_lectura_energia", "meter_id", "dim_medidor"),
    ("fact_lectura_energia", "date_key", "dim_tiempo"),
    ("fact_facturacion", "client_id", "dim_cliente"),
    ("fact_facturacion", "reading_id", "fact_lectura_energia"),
    ("fact_pago", "invoice_id", "fact_facturacion"),
    ("fact_pago", "client_id", "dim_cliente"),
    ("fact_energia_transformador_diaria", "transformer_id", "dim_transformador"),
    ("fact_energia_transformador_diaria", "date_key", "dim_tiempo"),
    ("fact_interrupcion", "circuit_id", "dim_circuito"),
    ("fact_interrupcion", "transformer_id", "dim_transformador"),
    ("fact_interrupcion", "zone_id", "dim_zona"),
    ("fact_interrupcion", "cause_id", "dim_causa"),
    ("fact_interrupcion", "date_key", "dim_tiempo"),
]

# Tablas de auditoría del ETL
AUDIT = {
    "aud_resumen_tablas": "[table_name] NVARCHAR(100) NOT NULL, [raw_rows] INT, [clean_rows] INT, "
                          "[gap_filled_rows] INT, [rejected_rows] INT, [reasons] NVARCHAR(2000)",
    "aud_calidad_datos": "[table_name] NVARCHAR(100) NOT NULL, [rule] NVARCHAR(200) NOT NULL, "
                         "[rows_affected] INT NOT NULL",
}

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _check(*names):
    for n in names:
        if not _IDENT.fullmatch(n):
            raise ValueError(f"Identificador inválido: {n!r}")


# ---------------------------------------------------------------------------
# Generación de T-SQL
# ---------------------------------------------------------------------------
def ddl_statements(schema="energrid"):
    """Devuelve (create_tables, add_fks, create_indexes). Las FK/índices se aplican DESPUÉS de cargar
    los datos (más rápido) y con WITH CHECK, de modo que SQL Server valida toda la integridad referencial."""
    _check(schema)
    creates = []
    for name, cols in TABLES.items():
        extra = f", CONSTRAINT [UQ_{name}] UNIQUE ({', '.join(f'[{c}]' for c in UNIQUE[name])})" \
            if name in UNIQUE else ""
        creates.append(f"CREATE TABLE [{schema}].[{name}] ({cols}, "
                       f"CONSTRAINT [PK_{name}] PRIMARY KEY CLUSTERED ([{PK[name]}]){extra})")
    for name, cols in AUDIT.items():
        creates.append(f"CREATE TABLE [{schema}].[{name}] ({cols})")
    fks = [f"ALTER TABLE [{schema}].[{t}] WITH CHECK ADD CONSTRAINT [FK_{t}_{c}] "
           f"FOREIGN KEY ([{c}]) REFERENCES [{schema}].[{p}] ([{PK[p]}])" for t, c, p in FKS]
    idx = []
    for t, c, _ in FKS:
        big = t.startswith("fact_") or t in ("dim_cliente", "dim_medidor")
        lead = {PK[t], UNIQUE.get(t, [None])[0]}          # ya cubiertas por PK/UNIQUE
        if big and c not in lead:
            idx.append(f"CREATE NONCLUSTERED INDEX [IX_{t}_{c}] ON [{schema}].[{t}] ([{c}])")
    idx.append(f"CREATE NONCLUSTERED INDEX [IX_fact_pago_payment_date] ON [{schema}].[fact_pago] ([payment_date])")
    return creates, fks, idx


def _drops(schema):
    return [f"DROP TABLE IF EXISTS [{schema}].[{n}]" for n in [*AUDIT, *reversed(list(TABLES))]]


def export_ddl(path="sql/energrid_sqlserver.sql", database="EnerGrid", schema="energrid"):
    """Escribe el script T-SQL completo (BD, esquema, tablas, PK, UNIQUE, FK, índices), listo para SSMS."""
    _check(database, schema)
    creates, fks, idx = ddl_statements(schema)
    go = "\nGO\n"
    parts = [f"-- EnerGrid BI Control Center - modelo dimensional para SQL Server\n"
             f"IF DB_ID(N'{database}') IS NULL CREATE DATABASE [{database}];",
             f"USE [{database}];",
             f"IF SCHEMA_ID(N'{schema}') IS NULL EXEC(N'CREATE SCHEMA [{schema}]');",
             "-- Limpieza (hijos -> padres)\n" + ";\n".join(_drops(schema)) + ";",
             "-- Tablas + PK + UNIQUE (padres -> hijos)\n" + ";\n".join(creates) + ";",
             "-- Relaciones (FK)\n" + ";\n".join(fks) + ";",
             "-- Índices de apoyo para joins/filtros de Power BI\n" + ";\n".join(idx) + ";"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(go.join(parts) + go, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Conexión y carga
# ---------------------------------------------------------------------------
def _conn_str(server, database, user, password, driver):
    if user and password is None:
        raise ValueError("La autenticacion SQL requiere una contraseña proporcionada en tiempo de ejecucion")
    auth = f"UID={user};PWD={password}" if user else "Trusted_Connection=yes"
    return (f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};{auth};"
            "Encrypt=yes;TrustServerCertificate=yes")


def _prep(name, df):
    """Tipos compatibles con pyodbc: fechas -> date, enteros -> Int64 (sin floats), NaN -> None se hace al final."""
    df = df.copy()
    cols = TABLES[name] if name in TABLES else AUDIT[name]
    for c in re.findall(r"\[(\w+)\] DATE\b", cols):
        if c in df:
            df[c] = pd.to_datetime(df[c]).dt.date
    for c in re.findall(r"\[(\w+)\] INT\b", cols):
        if c in df:
            df[c] = pd.to_numeric(df[c]).round().astype("Int64")
    return df


def _none(v):
    if v is None or v is pd.NA or v is pd.NaT or (isinstance(v, float) and v != v):
        return None
    return v


def _insert(cur, schema, name, df, chunk):
    df = _prep(name, df)
    cols = ", ".join(f"[{c}]" for c in df.columns)
    sql = f"INSERT INTO [{schema}].[{name}] ({cols}) VALUES ({', '.join(['?'] * len(df.columns))})"
    for i in range(0, len(df), chunk):
        part = df.iloc[i:i + chunk].astype(object)
        cur.executemany(sql, [tuple(_none(v) for v in r) for r in part.itertuples(index=False, name=None)])
    print(f"Cargada {schema}.{name}: {len(df):,} filas")


def load_sqlserver(clean, server, database="EnerGrid", schema="energrid", user=None, password=None,
                   driver="ODBC Driver 17 for SQL Server", report=None, audit=None, chunk=50_000):
    """Crea la BD/esquema, las tablas, carga los datos limpios y aplica FK e índices (una sola transacción).
    user=None -> autenticación de Windows; con user/password -> autenticación SQL."""
    import pyodbc
    _check(database, schema)
    master = pyodbc.connect(_conn_str(server, "master", user, password, driver), autocommit=True)
    master.execute(f"IF DB_ID(N'{database}') IS NULL CREATE DATABASE [{database}]")
    master.close()

    con = pyodbc.connect(_conn_str(server, database, user, password, driver))
    cur = con.cursor()
    cur.fast_executemany = True
    creates, fks, idx = ddl_statements(schema)
    try:
        cur.execute(f"IF SCHEMA_ID(N'{schema}') IS NULL EXEC(N'CREATE SCHEMA [{schema}]')")
        for s in _drops(schema) + creates:
            cur.execute(s)
        for name in TABLES:                                   # padres -> hijos
            _insert(cur, schema, name, clean[name], chunk)
        if report is not None:
            rep = pd.DataFrame({"table_name": report["table"], "raw_rows": report["raw_rows"],
                                "clean_rows": report["clean_rows"], "gap_filled_rows": report["gap_filled_rows"],
                                "rejected_rows": report["rejected_rows"],
                                "reasons": report["reasons"].astype(str).str.slice(0, 2000)})
            _insert(cur, schema, "aud_resumen_tablas", rep, chunk)
        if audit is not None and len(audit):
            aud = audit.rename(columns={"table": "table_name", "rows": "rows_affected"})
            _insert(cur, schema, "aud_calidad_datos", aud, chunk)
        for s in fks + idx:                                   # valida integridad con WITH CHECK
            cur.execute(s)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    print(f"Carga a SQL Server finalizada: {server}/{database}.{schema}")


if __name__ == "__main__":
    export_ddl("sql/energrid_sqlserver.sql")          # solo el script; la carga se lanza con: python sql/energrid_etl.py
