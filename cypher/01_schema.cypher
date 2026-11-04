// =============================================================================
// 01_schema.cypher —— 约束与索引（建图之前先建"骨架规矩"）
// -----------------------------------------------------------------------------
// 作用：
//   1. 唯一约束：保证 113 个实体不重复，重复 MERGE 只会命中同一条记录；
//   2. 索引：按 name 查实体、按 type 过滤类型时走索引，导入与查询都更快。
// 执行顺序：必须在导入数据之前执行（唯一约束会顺带创建 name 索引）。
// 说明：Neo4j 4.4 支持 IF NOT EXISTS，脚本可重复执行。
// =============================================================================

// 1. 实体 name 全局唯一（图谱里 name 就是业务主键，MERGE 依赖它）
CREATE CONSTRAINT entity_name_unique IF NOT EXISTS
FOR (n:Entity) REQUIRE n.name IS UNIQUE;

// 2. 按类型过滤（前端"只看算法/只看指标"、统计各类型数量时会用到）
CREATE INDEX entity_type_index IF NOT EXISTS
FOR (n:Entity) ON (n.type);

// 3. 查看约束与索引是否创建成功
SHOW CONSTRAINTS;
SHOW INDEXES;
