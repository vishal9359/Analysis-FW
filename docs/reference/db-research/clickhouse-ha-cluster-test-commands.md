Step 3 — Start the cluster and create a replicated table

docker compose up -d
docker compose ps                      # all 3 should be "running"

# Create a ReplicatedMergeTree table on BOTH nodes at once via ON CLUSTER:
docker exec -it clickhouse-01 clickhouse-client -q "
CREATE TABLE test_ha ON CLUSTER ha_cluster
(
    id UInt64,
    ts DateTime DEFAULT now(),
    msg String
)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/test_ha', '{replica}')
ORDER BY id;"
Step 4 — The HA tests
Test 1 — Replication works (write on node 1 → read on node 2)


docker exec -it clickhouse-01 clickhouse-client -q \
  "INSERT INTO test_ha (id, msg) VALUES (1, 'from node 1')"
docker exec -it clickhouse-02 clickhouse-client -q \
  "SELECT * FROM test_ha"
✅ Expect the row to appear on node 2.

Test 2 — Failover (kill a replica; the other still reads and writes)


docker stop clickhouse-01
docker exec -it clickhouse-02 clickhouse-client -q \
  "INSERT INTO test_ha (id, msg) VALUES (2, 'written while node1 was DOWN')"
docker exec -it clickhouse-02 clickhouse-client -q \
  "SELECT * FROM test_ha ORDER BY id"
✅ Node 2 serves both rows with node 1 down.

Test 3 — Auto-recovery (dead replica returns and catches up on its own)


docker start clickhouse-01
sleep 8
docker exec -it clickhouse-01 clickhouse-client -q \
  "SELECT * FROM test_ha ORDER BY id"
✅ Node 1 now shows id=2 too — it caught up automatically after coming back.

Test 4 — Inspect replication health


docker exec -it clickhouse-01 clickhouse-client -q \
  "SELECT database, table, is_readonly, absolute_delay, queue_size, active_replicas, total_replicas
   FROM system.replicas FORMAT Vertical"
✅ total_replicas=2, active_replicas=2, queue_size=0, is_readonly=0.

Test 5 (optional) — Why Keeper matters (shows the SPOF you'd fix in production)


docker stop clickhouse-keeper
docker exec -it clickhouse-01 clickhouse-client -q \
  "INSERT INTO test_ha (id, msg) VALUES (3,'no keeper')"   # → this FAILS/blocks
docker exec -it clickhouse-01 clickhouse-client -q \
  "SELECT count() FROM test_ha"                            # → reads still WORK
docker start clickhouse-keeper                             # writes resume after it's back
✅ Reads keep working without Keeper; writes to replicated tables block — which is exactly why production runs 3 Keepers (quorum tolerates 1 failure).

Cleanup

docker compose down -v      # -v also removes the data volumes