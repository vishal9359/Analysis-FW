"""Store adapters — one module per database, all behind `base.Store`.

ClickHouse is the production adapter; Memory backs the tests. Build one with
`factory.make_store(kind, ...)`; adding a database is a new module here plus one
line in the factory.
"""
