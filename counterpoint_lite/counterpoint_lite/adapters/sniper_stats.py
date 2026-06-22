import sqlite3

from ..observations import build_observation


def import_sniper_sqlite(path, mapping=None, prefix=None, **ci_kwargs):
    mapping = mapping or {}
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        if prefix is None:
            row = cur.execute("select prefixname from prefixes order by prefixid desc limit 1").fetchone()
            prefix = row[0] if row else None
        query = """
            select names.objectname, names.metricname, values.core, values.value
            from values
            join names on names.nameid = values.nameid
            join prefixes on prefixes.prefixid = values.prefixid
            where prefixes.prefixname = ?
        """
        values = {}
        for obj, metric, core, value in cur.execute(query, (prefix,)):
            key = f"{obj}.{metric}"
            if mapping and key not in mapping:
                continue
            name = mapping.get(key, key)
            values[name] = values.get(name, 0.0) + float(value)
        return build_observation("sniper-sqlite", path, values, aggregation=f"snapshot:{prefix}", **ci_kwargs)
    finally:
        conn.close()

