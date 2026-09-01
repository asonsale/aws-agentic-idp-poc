import os
import logging
from decimal import Decimal

import redis
from mcp.server.fastmcp import FastMCP

from retrieval import get_db_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MCPServer")

mcp = FastMCP("AWS Enterprise Multi-Tool Bridge (POC)", host="0.0.0.0", port=8001)

redis_client = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    port=int(os.environ.get("REDIS_PORT", 6379)),
    db=0,
    decode_responses=True,
)

CACHE_TTL_SECONDS = 300


@mcp.tool()
def query_customer_risk_multitenant(account_id: str, tenant_id: str) -> str:
    """Queries the RDS backend using PostgreSQL Row-Level Security (RLS) and Redis caching."""
    cache_key = f"mcp:risk:{tenant_id}:{account_id}"
    cached_data = redis_client.get(cache_key)
    if cached_data:
        logger.info(f"Redis cache hit for {cache_key}")
        return f"SUCCESS (CACHED): {cached_data}"

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SET LOCAL app.current_tenant = %s;", (tenant_id,))
        cursor.execute(
            "SELECT risk_status, balance_usd FROM customer_accounts WHERE account_id = %s",
            (account_id,),
        )
        result = cursor.fetchone()
        conn.close()

        if result:
            response_str = f"Tenant [{tenant_id}] Access Verified: Account {account_id} status is {result[0]}, Balance: ${result[1]}."
            redis_client.setex(cache_key, CACHE_TTL_SECONDS, response_str)
            return f"SUCCESS: {response_str}"
        return f"ACCESS_DENIED or RECORD_NOT_FOUND for tenant [{tenant_id}]."
    except Exception as e:
        return f"DATABASE ERROR: {str(e)}"


@mcp.tool()
def check_and_record_wire_transfer(account_id: str, tenant_id: str, transfer_amount: float) -> str:
    """
    Checks a wire transfer against the daily departmental limit AND records it
    atomically in the same transaction if approved.

    FIX vs. the original design: the original check_wire_transfer_limit read
    daily_transferred_today but never updated it, so the running total never
    grew and the control decayed to a no-op over time. This version uses
    SELECT ... FOR UPDATE to lock the row, then updates the counter in the
    same transaction as the check -- so two concurrent transfers on the same
    account can't both read the pre-transfer total and both get approved.
    """
    conn = None
    try:
        # FIX: max_daily_wire_limit and daily_transferred_today are Postgres
        # NUMERIC columns, which psycopg2 returns as Python Decimal objects.
        # transfer_amount arrives as a plain float from the tool-call
        # arguments (JSON has no Decimal type). Python refuses to add a
        # Decimal and a float directly, so every transfer -- approved or
        # rejected -- crashed with "unsupported operand type(s) for +:
        # 'decimal.Decimal' and 'float'" the moment the comparison below
        # ran. Casting once up front makes the whole function type-consistent.
        transfer_amount = Decimal(str(transfer_amount))

        conn = get_db_connection()
        # FIX: psycopg2 connections default to autocommit=False already, and
        # get_db_connection()'s call to register_vector() runs a query to
        # look up the vector type OID, which implicitly opens a transaction
        # on this connection. Re-setting conn.autocommit here -- even to the
        # same value -- calls psycopg2's set_session() internally, which
        # raises "set_session cannot be used inside a transaction" once a
        # transaction is already open. This line was both redundant (the
        # connection was never in autocommit mode to begin with) and the
        # actual cause of every wire-transfer tool call failing with a
        # DATABASE ERROR.
        cursor = conn.cursor()
        cursor.execute("SET LOCAL app.current_tenant = %s;", (tenant_id,))
        cursor.execute(
            "SELECT max_daily_wire_limit, daily_transferred_today FROM account_limits "
            "WHERE account_id = %s FOR UPDATE",
            (account_id,),
        )
        result = cursor.fetchone()

        if not result:
            conn.rollback()
            return f"POLICY_CHECK_FAILED: Account {account_id} has no configured limits."

        max_limit, transferred_today = result
        if (transferred_today + transfer_amount) > max_limit:
            conn.rollback()
            remaining = max_limit - transferred_today
            return (
                f"REJECTED: Transfer of ${transfer_amount} exceeds daily remaining limit "
                f"of ${remaining} (Daily Cap: ${max_limit}). Requires Risk Committee Override."
            )

        cursor.execute(
            "UPDATE account_limits SET daily_transferred_today = daily_transferred_today + %s "
            "WHERE account_id = %s",
            (transfer_amount, account_id),
        )
        conn.commit()
        return f"APPROVED: Transfer of ${transfer_amount} is within permissible limits and has been recorded."
    except Exception as e:
        if conn:
            conn.rollback()
        return f"DATABASE ERROR: {str(e)}"
    finally:
        if conn:
            conn.close()


@mcp.tool()
def check_erp_server_status(node_ip: str) -> str:
    """Queries internal ERP cluster diagnostics. NOTE: this remains a mocked
    stub in the POC (matches one hardcoded IP) -- it is not a real integration.
    Replace with a genuine health-check call before treating it as live."""
    cache_key = f"mcp:erp:{node_ip}"
    cached_status = redis_client.get(cache_key)
    if cached_status:
        return f"CACHED_DIAGNOSTIC: {cached_status}"

    status = (
        "Node ACTIVE. CPU: 12%, Memory: 42%, Replication Sync: OK"
        if node_ip == "10.0.4.21"
        else f"Node {node_ip} UNREACHABLE."
    )
    redis_client.setex(cache_key, 60, status)
    return status


if __name__ == "__main__":
    mcp.run(transport="sse")