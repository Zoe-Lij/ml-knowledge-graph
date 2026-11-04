// =============================================================================
// 02_import_loadcsv.cypher —— 用 LOAD CSV 批量导入（不写 Python 的导入方式）
// -----------------------------------------------------------------------------
// 【使用前提】Neo4j 只允许从它自己的 import 目录读文件：
//   1. 打开 Neo4j Desktop → 你的 DBMS → 右侧 "..." → Open folder → Import
//      （或直接把下面两个文件拷到 <neo4j安装目录>/import/ 下）
//   2. 把 data/entities.csv 和 data/relations.csv 复制进去
//   3. 确认文件名与下面 FROM 的路径完全一致（区分大小写）
//
// 【执行顺序】00_reset.cypher → 01_schema.cypher → 本文件 → 03_verify.cypher
//
// 【为什么节点用 entities.csv，关系用 relations.csv】
//   - entities.csv 带类型（决定标签 Concept/Algorithm/Model/Metric）和置信度；
//   - relations.csv 带 confidence / evidence / symmetric / description，
//     是三元组表的"超集"。若你坚持只用 entity_relation.csv，也能导入，
//     但关系上就没有置信度属性，前端会按 0.90 兜底（见 app/store.py）。
//
// 【中文乱码提醒】CSV 必须是 UTF-8（无 BOM）。用 Excel 打开另存会变成 GBK，
//   导入后中文会全变乱码——不要用 Excel 另存，直接复制原文件。
// =============================================================================


// -----------------------------------------------------------------------------
// 第一步：导入 113 个实体节点（按类型分 4 段，因为标签不能参数化）
// 说明：confidence 是"抽取置信度"（1.00 课程语料 / 0.90 Agent 扩展），
//       用 toFloat 转换，保证属性类型是数值而不是字符串，便于前端做数值过滤。
// -----------------------------------------------------------------------------

// 1.1 机器学习概念（38 个）→ 标签 Concept
LOAD CSV WITH HEADERS FROM 'file:///entities.csv' AS row
WITH row WHERE row.type = '机器学习概念'
MERGE (n:Entity:Concept {name: row.name})
SET n.id          = row.id,
    n.type        = row.type,
    n.en_name     = row.en_name,
    n.aliases     = row.aliases,
    n.description = row.description,
    n.color       = row.color,
    n.confidence  = toFloat(row.confidence),
    n.source      = row.source;

// 1.2 算法（34 个）→ 标签 Algorithm
LOAD CSV WITH HEADERS FROM 'file:///entities.csv' AS row
WITH row WHERE row.type = '算法'
MERGE (n:Entity:Algorithm {name: row.name})
SET n.id          = row.id,
    n.type        = row.type,
    n.en_name     = row.en_name,
    n.aliases     = row.aliases,
    n.description = row.description,
    n.color       = row.color,
    n.confidence  = toFloat(row.confidence),
    n.source      = row.source;

// 1.3 模型（23 个）→ 标签 Model
LOAD CSV WITH HEADERS FROM 'file:///entities.csv' AS row
WITH row WHERE row.type = '模型'
MERGE (n:Entity:Model {name: row.name})
SET n.id          = row.id,
    n.type        = row.type,
    n.en_name     = row.en_name,
    n.aliases     = row.aliases,
    n.description = row.description,
    n.color       = row.color,
    n.confidence  = toFloat(row.confidence),
    n.source      = row.source;

// 1.4 评价指标（18 个）→ 标签 Metric
LOAD CSV WITH HEADERS FROM 'file:///entities.csv' AS row
WITH row WHERE row.type = '评价指标'
MERGE (n:Entity:Metric {name: row.name})
SET n.id          = row.id,
    n.type        = row.type,
    n.en_name     = row.en_name,
    n.aliases     = row.aliases,
    n.description = row.description,
    n.color       = row.color,
    n.confidence  = toFloat(row.confidence),
    n.source      = row.source;


// -----------------------------------------------------------------------------
// 第二步：导入 257 条关系（按关系类型分 5 段，理由同上：关系类型不能参数化）
// 关系类型是中文，必须用反引号包裹：[:`属于`]
// 每条边写入 4 个属性：description（人话描述）、confidence（置信度）、
//                     evidence（依据来源）、symmetric（是否对称）
// -----------------------------------------------------------------------------

// 2.1 属于（93 条）
LOAD CSV WITH HEADERS FROM 'file:///relations.csv' AS row
WITH row WHERE row.relation = '属于'
MATCH (a:Entity {name: row.head_name})
MATCH (b:Entity {name: row.tail_name})
MERGE (a)-[r:`属于`]->(b)
SET r.description = row.description,
    r.confidence  = toFloat(row.confidence),
    r.evidence    = row.evidence,
    r.symmetric   = (row.symmetric = 'true');

// 2.2 衍生自（22 条）
LOAD CSV WITH HEADERS FROM 'file:///relations.csv' AS row
WITH row WHERE row.relation = '衍生自'
MATCH (a:Entity {name: row.head_name})
MATCH (b:Entity {name: row.tail_name})
MERGE (a)-[r:`衍生自`]->(b)
SET r.description = row.description,
    r.confidence  = toFloat(row.confidence),
    r.evidence    = row.evidence,
    r.symmetric   = (row.symmetric = 'true');

// 2.3 基于（29 条）
LOAD CSV WITH HEADERS FROM 'file:///relations.csv' AS row
WITH row WHERE row.relation = '基于'
MATCH (a:Entity {name: row.head_name})
MATCH (b:Entity {name: row.tail_name})
MERGE (a)-[r:`基于`]->(b)
SET r.description = row.description,
    r.confidence  = toFloat(row.confidence),
    r.evidence    = row.evidence,
    r.symmetric   = (row.symmetric = 'true');

// 2.4 可对比（47 条，逻辑对称，打上 symmetric 标记供前端画双向箭头）
LOAD CSV WITH HEADERS FROM 'file:///relations.csv' AS row
WITH row WHERE row.relation = '可对比'
MATCH (a:Entity {name: row.head_name})
MATCH (b:Entity {name: row.tail_name})
MERGE (a)-[r:`可对比`]->(b)
SET r.description = row.description,
    r.confidence  = toFloat(row.confidence),
    r.evidence    = row.evidence,
    r.symmetric   = true;

// 2.5 应用于（66 条）
LOAD CSV WITH HEADERS FROM 'file:///relations.csv' AS row
WITH row WHERE row.relation = '应用于'
MATCH (a:Entity {name: row.head_name})
MATCH (b:Entity {name: row.tail_name})
MERGE (a)-[r:`应用于`]->(b)
SET r.description = row.description,
    r.confidence  = toFloat(row.confidence),
    r.evidence    = row.evidence,
    r.symmetric   = (row.symmetric = 'true');


// -----------------------------------------------------------------------------
// 第三步：导入后自检（期望：113 个节点 / 257 条关系）
// -----------------------------------------------------------------------------
MATCH (n:Entity) RETURN count(n) AS `节点数`;
MATCH ()-[r]->() RETURN count(r) AS `关系数`;

// 置信度分布自检（期望 0.98:33 / 0.92:27 / 0.86:120 / 0.78:77）
MATCH ()-[r]->()
RETURN r.confidence AS `置信度`, count(*) AS `条数`, head(collect(r.evidence)) AS `依据`
ORDER BY `置信度` DESC;
