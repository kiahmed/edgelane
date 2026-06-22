#!/usr/bin/env python3
"""EdgeLane Supabase data admin — list tables and do CRUD without writing SQL.

Talks to the project through the Supabase Management API `database/query`
endpoint (the SAME path tools/db_push.py uses) — so there's NO direct Postgres
connection, no psql, no extra deps, just stdlib urllib. Queries run with
elevated rights (RLS is bypassed), so this is an OPERATOR tool: keep it
server-side, never ship it anywhere a browser can reach.

Config comes from deploy/.env:
  SUPABASE_PROJECT_REF   project ref (e.g. wfezpfswpywsmbnjrbri)
  SUPABASE_ACCESS_TOKEN  Management API token (sbp_…)

Quick start:
  python3 tools/supabase_admin.py tables                 # all tables + row counts + last-updated
  python3 tools/supabase_admin.py describe broker_configs # columns, types, PK
  python3 tools/supabase_admin.py list profiles --limit 5
  python3 tools/supabase_admin.py list profiles --where plan=pro --order created_at:desc
  python3 tools/supabase_admin.py insert app_config --set key=foo --set value=bar
  python3 tools/supabase_admin.py update profiles --set plan=pro --where email~gmail
  python3 tools/supabase_admin.py delete user_settings --where user_id=<uuid>
  python3 tools/supabase_admin.py sql "select count(*) from auth.users"

Safety:
  --dry-run     print the SQL it WOULD run, execute nothing (works on every cmd)
  --yes / -y    skip the confirm prompt on insert/update/delete
  update/delete REFUSE to run without a --where unless you pass --all
  Non-interactive shells must pass --yes for destructive ops (no prompt to answer).
  delete prints a CASCADE-IMPACT PREVIEW first: it walks the live FK graph and
    shows what else the delete removes (ON DELETE CASCADE, recursively), what gets
    nulled (SET NULL/DEFAULT), and any RESTRICT/NO ACTION refs that would BLOCK it.
    The DB enforces all of this — the preview just makes it visible. --no-cascade
    skips it. (Works under --dry-run too; the counts are read-only.)
  Global flags (-n/-y/--json/--wide/-x) may come before OR after the subcommand.

Output:
  Bordered table that WRAPS long cells to fit your terminal (never overflows).
  Too many columns to fit a grid (e.g. `list auth.users`) → auto record view
  (psql \\x style, one field per line). Force it with -x/--expanded; --wide keeps
  full untrimmed column widths; --json emits raw JSON.

Value syntax for --set / --where (no quoting-SQL needed):
  col=val      auto-typed: 42 / 3.14 → number, true/false → bool, null → NULL,
               {..}/[..] → jsonb, anything else → quoted text
  col=s:val    force text literal (e.g. an all-digits account id: acct=s:0012)
  col=r:expr   raw SQL expression, used verbatim (e.g. updated_at=r:now())
  --where ops: =  !=  >  <  >=  <=  and  ~  (ILIKE %val%, substring match)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "deploy" / ".env"
# Default urllib UA gets Cloudflare-1010 (WAF) blocked; pose as a browser.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WHERE_OPS = ("!=", ">=", "<=", "=", ">", "<", "~")   # longest-first for matching


# ── env + transport ─────────────────────────────────────────────────────────
def load_env(path: Path) -> dict:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def run_sql(ref: str, token: str, sql: str):
    """POST one SQL statement; return parsed JSON (list of row dicts, or [])."""
    url = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    req = urllib.request.Request(
        url, method="POST", data=json.dumps({"query": sql}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500]
        raise SystemExit(f"ERROR HTTP {e.code} from Supabase: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"ERROR reaching Supabase: {e}")
    if not body.strip():
        return []
    data = json.loads(body)
    # Management API may wrap an error as {"message": "..."} instead of a row list.
    if isinstance(data, dict) and "message" in data and "error" in str(data).lower():
        raise SystemExit(f"ERROR from Supabase: {data['message']}")
    return data if isinstance(data, list) else [data]


# ── identifier + literal helpers ─────────────────────────────────────────────
def ident(name: str) -> str:
    """Validate then double-quote an identifier (table/column)."""
    base = name.split(".")[-1]
    if not _IDENT.match(base):
        raise SystemExit(f"ERROR unsafe identifier: {name!r}")
    if "." in name:
        sch, _, tbl = name.partition(".")
        if not _IDENT.match(sch):
            raise SystemExit(f"ERROR unsafe schema: {sch!r}")
        return f'"{sch}"."{tbl}"'
    return f'"{name}"'


def literal(val: str) -> str:
    """Turn a CLI value token into a SQL literal/expression. See module docstring."""
    if val.startswith("r:"):                       # raw expression, verbatim
        return val[2:]
    if val.startswith("s:"):                        # forced text
        return "'" + val[2:].replace("'", "''") + "'"
    low = val.lower()
    if low == "null":
        return "null"
    if low in ("true", "false"):
        return low
    if re.fullmatch(r"-?\d+", val) or re.fullmatch(r"-?\d+\.\d+", val):
        return val
    if val[:1] in ("{", "["):                       # json/jsonb
        return "'" + val.replace("'", "''") + "'::jsonb"
    return "'" + val.replace("'", "''") + "'"


def parse_set(pairs: list[str]) -> list[tuple[str, str]]:
    out = []
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"ERROR --set expects col=value, got {p!r}")
        col, _, val = p.partition("=")
        out.append((col.strip(), val))
    return out


def parse_where(pairs: list[str]) -> str:
    """Build a WHERE body (no leading 'where') from col<op>val tokens, AND-ed."""
    clauses = []
    for p in pairs or []:
        op = next((o for o in _WHERE_OPS if o in p), None)
        if not op:
            raise SystemExit(f"ERROR --where needs an operator (= != > < >= <= ~): {p!r}")
        col, _, val = p.partition(op)
        col, val = col.strip(), val.strip()
        if op == "~":
            clauses.append(f"{ident(col)} ILIKE '%{val.replace(chr(39), chr(39)*2)}%'")
        else:
            clauses.append(f"{ident(col)} {op} {literal(val)}")
    return " and ".join(clauses)


# ── pretty printer ───────────────────────────────────────────────────────────
_MIN_COL = 10                       # don't shrink a grid column below this
_TERM = lambda: shutil.get_terminal_size((120, 24)).columns


def _cellstr(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ": "), ensure_ascii=False)
    return str(v)


def _wrap(text: str, width: int) -> list[str]:
    """Wrap into lines of <= width, preserving existing newlines, breaking long tokens."""
    width = max(1, width)
    lines: list[str] = []
    for para in text.split("\n"):
        if para == "":
            lines.append("")
        else:
            lines.extend(textwrap.wrap(para, width=width, break_long_words=True,
                                       break_on_hyphens=False) or [""])
    return lines or [""]


def _count_line(n: int) -> str:
    return f"({n} row{'s' if n != 1 else ''})"


def _grid(cols, data, widths) -> str:
    def bar(l, m, r):
        return l + m.join("─" * (w + 2) for w in widths) + r

    def block(cells_lines):
        h = max(len(c) for c in cells_lines)
        rows_out = []
        for i in range(h):
            segs = [" " + (c[i] if i < len(c) else "").ljust(widths[j]) + " "
                    for j, c in enumerate(cells_lines)]
            rows_out.append("│" + "│".join(segs) + "│")
        return rows_out

    wrapped = [[_wrap(row[i], widths[i]) for i in range(len(cols))] for row in data]
    multiline = any(max(len(c) for c in rl) > 1 for rl in wrapped)
    out = [bar("┌", "┬", "┐")]
    out += block([_wrap(c, widths[i]) for i, c in enumerate(cols)])
    out.append(bar("├", "┼", "┤"))
    for ri, rl in enumerate(wrapped):
        if ri and multiline:                       # separate records only when cells wrap
            out.append(bar("├", "┼", "┤"))
        out += block(rl)
    out.append(bar("└", "┴", "┘"))
    out.append(_count_line(len(data)))
    return "\n".join(out)


def _expanded(cols, data, term_w) -> str:
    keyw = min(max((len(c) for c in cols), default=3), 22)
    valw = max(20, term_w - keyw - 3)
    out = []
    for idx, row in enumerate(data, 1):
        head = f"─[ row {idx} ]"
        out.append(head + "─" * max(0, term_w - len(head)))
        for i, c in enumerate(cols):
            vlines = _wrap(row[i], valw)
            out.append(f"{c[:keyw].ljust(keyw)} │ {vlines[0]}")
            for extra in vlines[1:]:
                out.append(f"{' ' * keyw} │ {extra}")
    out.append(_count_line(len(data)))
    return "\n".join(out)


def render(rows: list, wide: bool = False, expanded: bool = False) -> str:
    """Bordered, wrapping table. Falls back to expanded (record) layout when too
    many columns to fit the terminal; force it with expanded=True. wide=True keeps
    full natural column widths (no wrapping/capping, may overflow)."""
    if not rows:
        return "(0 rows)"
    if not isinstance(rows[0], dict):
        return "\n".join(str(r) for r in rows)
    cols = list(rows[0].keys())
    data = [[_cellstr(r.get(c)) for c in cols] for r in rows]
    term_w = _TERM()

    def maxlen(s):
        return max((len(x) for x in s.split("\n")), default=0)
    natural = [max(len(cols[i]), max((maxlen(row[i]) for row in data), default=0))
               for i in range(len(cols))]

    if expanded:
        return _expanded(cols, data, term_w)

    avail = term_w - (3 * len(cols) + 1)           # borders + per-cell padding
    if wide or sum(natural) <= avail:
        return _grid(cols, data, natural)

    # too wide for natural widths → shrink the biggest columns, then wrap.
    floor = sum(min(n, _MIN_COL) for n in natural)
    if floor > avail:                              # can't even fit minimums → records
        return _expanded(cols, data, term_w)
    widths = natural[:]
    while sum(widths) > avail:
        i = widths.index(max(widths))
        if widths[i] <= _MIN_COL:
            break
        widths[i] -= 1
    return _grid(cols, data, widths)


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_tables(ctx, a):
    # introspect public columns to pick a per-table "last updated" timestamp col
    cols = run_sql(*ctx, """
        select table_name, column_name
        from information_schema.columns
        where table_schema='public'
          and table_name in (select table_name from information_schema.tables
                             where table_schema='public' and table_type='BASE TABLE')
        order by table_name, ordinal_position;""")
    by_tbl: dict[str, list] = {}
    for r in cols:
        by_tbl.setdefault(r["table_name"], []).append(r["column_name"])
    if not by_tbl:
        print("(no public tables)")
        return
    # one round-trip: UNION ALL count(*) + max(ts) per table
    parts = []
    for t, c in by_tbl.items():
        ts = "updated_at" if "updated_at" in c else "created_at" if "created_at" in c else None
        lastsel = f"max({ident(ts)})::text" if ts else "null"
        parts.append(f"select '{t}' as \"table\", count(*) as rows, {lastsel} as last_updated "
                     f"from {ident('public.'+t)}")
    # auth.users for visibility (managed by Supabase; CRUD it only deliberately)
    parts.append("select 'auth.users' as \"table\", count(*) as rows, "
                 "max(coalesce(updated_at, created_at))::text as last_updated from auth.users")
    rows = run_sql(*ctx, " union all ".join(parts) + " order by \"table\";")
    print(render(rows))


def cmd_describe(ctx, a):
    tbl = a.table
    sch, _, name = tbl.partition(".") if "." in tbl else ("public", "", tbl)
    name = name or tbl
    rows = run_sql(*ctx, f"""
        select c.column_name, c.data_type,
               c.is_nullable, coalesce(c.column_default,'') as default,
               case when pk.column_name is not null then 'PK' else '' end as key
        from information_schema.columns c
        left join (
          select kcu.column_name
          from information_schema.table_constraints tc
          join information_schema.key_column_usage kcu
            on kcu.constraint_name = tc.constraint_name
           and kcu.table_schema = tc.table_schema
          where tc.table_schema='{sch}' and tc.table_name='{name}'
            and tc.constraint_type='PRIMARY KEY'
        ) pk on pk.column_name = c.column_name
        where c.table_schema='{sch}' and c.table_name='{name}'
        order by c.ordinal_position;""")
    if not rows:
        raise SystemExit(f"ERROR no such table: {tbl}")
    print(f"{sch}.{name}")
    print(render(rows))


def _select_sql(a) -> str:
    cols = ", ".join(ident(c) for c in a.cols.split(",")) if a.cols else "*"
    sql = f"select {cols} from {ident(a.table)}"
    where = parse_where(a.where)
    if where:
        sql += f" where {where}"
    if a.order:
        oc, _, od = a.order.partition(":")
        sql += f" order by {ident(oc)} {'desc' if od.lower()=='desc' else 'asc'}"
    sql += f" limit {int(a.limit)};"
    return sql


def cmd_list(ctx, a):
    sql = _select_sql(a)
    if a.dry_run:
        print(sql); return
    rows = run_sql(*ctx, sql)
    print(json.dumps(rows, indent=2) if a.json else render(rows, wide=a.wide, expanded=a.expanded))


def cmd_count(ctx, a):
    where = parse_where(a.where)
    sql = f"select count(*) as count from {ident(a.table)}" + (f" where {where}" if where else "") + ";"
    if a.dry_run:
        print(sql); return
    print(run_sql(*ctx, sql)[0]["count"])


def cmd_insert(ctx, a):
    sets = parse_set(a.set)
    if not sets:
        raise SystemExit("ERROR insert needs at least one --set col=value")
    cols = ", ".join(ident(c) for c, _ in sets)
    vals = ", ".join(literal(v) for _, v in sets)
    sql = f"insert into {ident(a.table)} ({cols}) values ({vals}) returning *;"
    _exec_write(ctx, a, sql, "insert", None)


def cmd_update(ctx, a):
    sets = parse_set(a.set)
    if not sets:
        raise SystemExit("ERROR update needs at least one --set col=value")
    where = parse_where(a.where)
    if not where and not a.all:
        raise SystemExit("REFUSING to update every row. Add --where, or pass --all on purpose.")
    assigns = ", ".join(f"{ident(c)} = {literal(v)}" for c, v in sets)
    sql = f"update {ident(a.table)} set {assigns}" + (f" where {where}" if where else "") + " returning *;"
    _exec_write(ctx, a, sql, "update", where if where else None)


def cmd_delete(ctx, a):
    where = parse_where(a.where)
    if not where and not a.all:
        raise SystemExit("REFUSING to delete every row. Add --where, or pass --all on purpose.")
    sql = f"delete from {ident(a.table)}" + (f" where {where}" if where else "") + " returning *;"
    _exec_write(ctx, a, sql, "delete", where if where else None)


def cmd_sql(ctx, a):
    if a.dry_run:
        print(a.query); return
    rows = run_sql(*ctx, a.query)
    print(json.dumps(rows, indent=2) if a.json else render(rows, wide=a.wide, expanded=a.expanded))


# ── cascade impact preview (for delete) ──────────────────────────────────────
_DEL_RULE = {"c": "CASCADE", "a": "NO ACTION", "r": "RESTRICT", "n": "SET NULL", "d": "SET DEFAULT"}
# Single-column FKs across all schemas, with their ON DELETE action. Composite
# FKs (rare; none here) use only the first column — good enough for a row-count
# estimate, and noted as such in the preview header.
_FK_GRAPH_SQL = """
select cn.nspname||'.'||cl.relname  as child,
       ca.attname                   as child_col,
       pn.nspname||'.'||pcl.relname as parent,
       pa.attname                   as parent_col,
       c.confdeltype                as del
from pg_constraint c
join pg_class      cl  on cl.oid  = c.conrelid
join pg_namespace  cn  on cn.oid  = cl.relnamespace
join pg_class      pcl on pcl.oid = c.confrelid
join pg_namespace  pn  on pn.oid  = pcl.relnamespace
join pg_attribute  ca  on ca.attrelid = c.conrelid  and ca.attnum = c.conkey[1]
join pg_attribute  pa  on pa.attrelid = c.confrelid and pa.attnum = c.confkey[1]
where c.contype='f';"""


def _fk_graph(ctx) -> dict:
    graph: dict[str, list] = {}
    for r in run_sql(*ctx, _FK_GRAPH_SQL):
        graph.setdefault(r["parent"], []).append(r)
    return graph


def _print_cascade_preview(ctx, table, where):
    """Walk the FK graph from `table` and report what a delete would touch."""
    graph = _fk_graph(ctx)
    canon = table if "." in table else f"public.{table}"
    predicate = where or "true"
    deletes, mutates, blockers = [], [], []

    def walk(parent, parent_pred, depth, path):
        if depth > 8:
            return
        for fk in graph.get(parent, []):
            child, ccol, pcol = fk["child"], fk["child_col"], fk["parent_col"]
            rule = _DEL_RULE.get(fk["del"], fk["del"])
            child_pred = (f"{ident(ccol)} in "
                          f"(select {ident(pcol)} from {ident(parent)} where {parent_pred})")
            n = run_sql(*ctx, f"select count(*) as c from {ident(child)} where {child_pred};")[0]["c"]
            if not n:
                continue
            pad = "  " * depth
            if rule == "CASCADE":
                deletes.append(f"  {pad}└─ {child}: delete {n}")
                if child not in path:                 # guard self-referential cycles
                    walk(child, child_pred, depth + 1, path | {child})
            elif rule in ("SET NULL", "SET DEFAULT"):
                mutates.append(f"  {pad}└─ {child}: {n} → {rule.lower()} (row kept)")
            else:                                      # NO ACTION / RESTRICT
                blockers.append(f"  {child}: {n} row(s) [{rule}]")

    walk(canon, predicate, 0, {canon})

    if not (deletes or mutates or blockers):
        print("cascade: no dependent rows — nothing else is affected")
        return
    if deletes:
        print("cascade — will ALSO delete:")
        print("\n".join(deletes))
    if mutates:
        print("cascade — will null/default (kept):")
        print("\n".join(mutates))
    if blockers:
        print("⚠ BLOCKERS — these references are RESTRICT/NO ACTION; the delete will FAIL:")
        print("\n".join(blockers))


def _exec_write(ctx, a, sql, verb, where):
    print(f"SQL> {sql}")
    if verb == "delete" and not getattr(a, "no_cascade", False):
        _print_cascade_preview(ctx, a.table, where)
    if a.dry_run:
        print("[dry-run] nothing executed")
        return
    if verb in ("update", "delete"):
        cnt_sql = f"select count(*) as count from {ident(a.table)}" + (f" where {where}" if where else "") + ";"
        n = run_sql(*ctx, cnt_sql)[0]["count"]
        print(f"matches {n} row(s)")
        if n == 0:
            print("nothing to do"); return
    if not a.yes:
        if not sys.stdin.isatty():
            raise SystemExit("ERROR refusing destructive op without confirmation; pass --yes "
                             "(no TTY to prompt on).")
        if input(f"proceed with {verb}? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted"); return
    rows = run_sql(*ctx, sql)
    print(f"✓ {verb} affected {len(rows)} row(s)")
    if rows:
        print(render(rows, wide=a.wide, expanded=a.expanded))


# ── argparse ─────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    # Global flags live on a parent parser added to EVERY subparser (with a
    # SUPPRESS default so the sub copy never clobbers a value parsed at the top
    # level) AND on the main parser — so `-y delete …` and `delete … -y` both work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", "-n", action="store_true", default=argparse.SUPPRESS,
                        help="print SQL, execute nothing")
    common.add_argument("--yes", "-y", action="store_true", default=argparse.SUPPRESS,
                        help="skip confirm on write ops")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="raw JSON output (list/sql)")
    common.add_argument("--wide", action="store_true", default=argparse.SUPPRESS,
                        help="full natural column widths (no wrapping; may overflow)")
    common.add_argument("--expanded", "-x", action="store_true", default=argparse.SUPPRESS,
                        help="one record per block (psql \\x style); best for wide tables")

    p = argparse.ArgumentParser(
        prog="supabase_admin.py", description=__doc__, parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def new(name, **kw):
        kw.setdefault("parents", []).append(common)
        return sub.add_parser(name, **kw)

    new("tables", help="list all tables: row counts + last-updated").set_defaults(fn=cmd_tables)

    d = new("describe", aliases=["desc"], help="columns, types, PK for a table")
    d.add_argument("table"); d.set_defaults(fn=cmd_describe)

    def add_filters(sp):
        sp.add_argument("table")
        sp.add_argument("--where", "-w", action="append", metavar="col=val",
                        help="filter (repeatable, AND-ed); ops = != > < >= <= ~ (ILIKE)")

    ls = new("list", aliases=["select", "get"], help="read rows",
             epilog="SQL: select * from <table> where ... order by .. limit ..",
             formatter_class=argparse.RawDescriptionHelpFormatter)
    add_filters(ls)
    ls.add_argument("--cols", help="comma-separated columns (default *)")
    ls.add_argument("--order", metavar="col[:asc|desc]", help="sort, e.g. created_at:desc")
    ls.add_argument("--limit", default=50, help="max rows (default 50)")
    ls.set_defaults(fn=cmd_list)

    c = new("count", help="count rows (with optional --where)")
    add_filters(c); c.set_defaults(fn=cmd_count)

    ins = new("insert", help="insert a row",
              epilog="SQL: insert into <table> (cols) values (vals) returning *",
              formatter_class=argparse.RawDescriptionHelpFormatter)
    ins.add_argument("table")
    ins.add_argument("--set", "-s", action="append", metavar="col=val", help="column value (repeatable)")
    ins.set_defaults(fn=cmd_insert)

    up = new("update", help="update rows matching --where",
             epilog="SQL: update <table> set col=val where ... returning *",
             formatter_class=argparse.RawDescriptionHelpFormatter)
    add_filters(up)
    up.add_argument("--set", "-s", action="append", metavar="col=val", help="column to set (repeatable)")
    up.add_argument("--all", action="store_true", help="allow updating EVERY row (no --where)")
    up.set_defaults(fn=cmd_update)

    dl = new("delete", aliases=["del", "rm"], help="delete rows matching --where",
             epilog="SQL: delete from <table> where ... returning *",
             formatter_class=argparse.RawDescriptionHelpFormatter)
    add_filters(dl)
    dl.add_argument("--all", action="store_true", help="allow deleting EVERY row (no --where)")
    dl.add_argument("--no-cascade", action="store_true",
                    help="skip the cascade-impact preview (shown by default)")
    dl.set_defaults(fn=cmd_delete)

    s = new("sql", help="run raw SQL (escape hatch)")
    s.add_argument("query"); s.set_defaults(fn=cmd_sql)
    return p


def main() -> int:
    a = build_parser().parse_args()
    env = load_env(ENV_FILE)
    ref = env.get("SUPABASE_PROJECT_REF") or env.get("SUPABASE_URL", "").split("//")[-1].split(".")[0]
    token = env.get("SUPABASE_ACCESS_TOKEN", "")
    if not ref or not token:
        raise SystemExit("ERROR set SUPABASE_PROJECT_REF and SUPABASE_ACCESS_TOKEN in deploy/.env")
    ctx = (ref, token)
    # make global flags visible to handlers that read them off `a`
    for fl in ("dry_run", "yes", "json", "wide", "expanded"):
        setattr(a, fl, getattr(a, fl, False))
    a.fn(ctx, a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
