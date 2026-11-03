"""Upper bounds for integer query and path params.

Python ints are arbitrary-precision, so a bare `int` annotation accepts values far
wider than the int4 columns and int8 OFFSET clauses they eventually reach. Without an
upper bound the overflow surfaces as a database error — a 500 — instead of a 422.
"""

# Keeps `(page - 1) * page_size` inside int8 for any page size below, and is already
# far deeper than any reachable result set.
MAX_PAGE = 1_000_000

# Applied only where a route declared no cap of its own; a route with a tighter
# explicit limit keeps it.
MAX_PAGE_SIZE = 1_000

# Postgres `integer`, the column type behind every version number.
MAX_INT4 = 2_147_483_647
