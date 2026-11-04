// =============================================================================
// 00_reset.cypher —— 清空数据库（演示前重置、重复导入前清理）
// -----------------------------------------------------------------------------
// 用途：把图数据库恢复成"空库"状态，保证每次演示/答辩都从干净状态开始。
// 执行方式（三选一）：
//   1. Neo4j Browser 里粘贴执行；
//   2. cypher-shell -a bolt://localhost:7687 -u neo4j -p 你的Neo4j密码 -f cypher/00_reset.cypher
//   3. Python：python scripts/import_neo4j.py --reset
// 警告：DETACH DELETE 会删除库里所有节点与关系，只在本项目的课程环境使用！
// =============================================================================

// 1. 删除所有节点及其关系（DETACH 表示连带删除关系）
MATCH (n)
DETACH DELETE n;

// 2. 删除本项目建立的约束与索引（IF EXISTS 保证重复执行不报错）
DROP CONSTRAINT entity_name_unique IF EXISTS;
DROP INDEX entity_type_index IF EXISTS;

// 3. 确认已清空：应返回 0
MATCH (n) RETURN count(n) AS node_count;
