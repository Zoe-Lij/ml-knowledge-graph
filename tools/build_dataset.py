#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_dataset.py
----------------
机器学习领域知识图谱（ML-KG）数据集生成脚本。

作用：
    1. 在本文件中以「人工结构化 + 规则化」的方式维护实体与关系（单一数据源）；
    2. 一键导出三种格式，供后续建库 / 后端 / 前端复用：
         data/entities.csv   实体表（Neo4j LOAD CSV 可直接导入）
         data/relations.csv  关系表（Neo4j LOAD CSV 可直接导入）
         data/kg.json        聚合数据（Python 后端 / D3.js 前端直接读取）
    3. 导出前做一轮轻量自检，避免类型越界、悬空关系、重复边等问题。

设计约束（课程设计硬性要求）：
    - 实体类型只有 4 类：算法 / 模型 / 评价指标 / 机器学习概念
    - 关系类型只有 5 类：属于 / 衍生自 / 基于 / 可对比 / 应用于
    - 实体总数控制在 80~120 个，保证图可观、可讲、可维护

运行方式：
    python tools/build_dataset.py
"""

import csv
import json
import os
import sys
from collections import Counter

# 项目根目录（本文件位于 <root>/tools 下）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

# ---------------------------------------------------------------------------
# 一、元信息：实体类型、关系类型、可视化配色（前后端共用同一份约定）
# ---------------------------------------------------------------------------
ENTITY_TYPES = ["机器学习概念", "算法", "模型", "评价指标"]

RELATION_TYPES = ["属于", "衍生自", "基于", "可对比", "应用于"]

# Neo4j 标签映射：中文类型 -> 英文标签（Cypher 里用英文标签，属性里保留中文类型）
TYPE_TO_LABEL = {
    "机器学习概念": "Concept",
    "算法": "Algorithm",
    "模型": "Model",
    "评价指标": "Metric",
}

# 前端配色（浅色主题，色相区分 4 类实体，答辩投屏可读）
TYPE_COLOR = {
    "机器学习概念": "#3B6FD4",
    "算法": "#7C5CD6",
    "模型": "#E08A2E",
    "评价指标": "#2E9E6B",
}

# ---------------------------------------------------------------------------
# 二、实体表
# 字段：(id, 名称, 类型, 英文名, 别名(多个用 | 分隔), 一句话描述)
# 说明：别名是"自然语言问答"的关键资产——用户很少说出标准名，
#       例如"随机森林算法""RF""逻辑斯蒂回归"，都要能命中同一实体。
# ---------------------------------------------------------------------------
ENTITIES = [
    # ---------------- 机器学习概念（38） ----------------
    ("C01", "监督学习", "机器学习概念", "Supervised Learning", "有监督学习|supervised learning", "利用带标签数据学习输入到输出映射的学习范式"),
    ("C02", "无监督学习", "机器学习概念", "Unsupervised Learning", "unsupervised learning|无监督", "从无标签数据中发现结构与模式的范式"),
    ("C03", "半监督学习", "机器学习概念", "Semi-Supervised Learning", "semi-supervised|半监督", "用少量标签数据与大量无标签数据联合训练的范式"),
    ("C04", "自监督学习", "机器学习概念", "Self-Supervised Learning", "self-supervised|自监督", "从数据自身构造监督信号进行预训练的范式"),
    ("C05", "强化学习", "机器学习概念", "Reinforcement Learning", "RL|reinforcement learning", "智能体与环境交互、依据奖励信号学习策略的范式"),
    ("C06", "集成学习", "机器学习概念", "Ensemble Learning", "ensemble|集成方法", "组合多个基学习器以提升泛化能力的范式"),
    ("C07", "深度学习", "机器学习概念", "Deep Learning", "DL|deep learning|深度神经网络", "以多层神经网络自动学习层次化特征的范式"),
    ("C08", "迁移学习", "机器学习概念", "Transfer Learning", "transfer learning|领域自适应", "把源域学到的知识迁移到目标域的范式"),
    ("C09", "分类任务", "机器学习概念", "Classification", "分类|classification|二分类|多分类", "预测样本所属离散类别的任务"),
    ("C10", "回归任务", "机器学习概念", "Regression", "回归|regression|数值预测", "预测连续数值目标的任务"),
    ("C11", "聚类任务", "机器学习概念", "Clustering", "聚类|clustering|分群", "按相似度把无标签样本划分为若干簇的任务"),
    ("C12", "降维任务", "机器学习概念", "Dimensionality Reduction", "降维|dimensionality reduction|特征压缩", "在保留主要信息的前提下压缩特征维度的任务"),
    ("C13", "异常检测任务", "机器学习概念", "Anomaly Detection", "异常检测|outlier detection|离群点检测", "识别与大多数样本显著不同的异常样本的任务"),
    ("C14", "排序任务", "机器学习概念", "Learning to Rank", "排序|ltr|learning to rank|推荐排序", "按相关性对候选集合进行排序的任务"),
    ("C15", "序列预测任务", "机器学习概念", "Sequence Prediction", "序列预测|时序预测|sequence prediction", "对时序或文本序列做下一时刻预测的任务"),
    ("C16", "过拟合", "机器学习概念", "Overfitting", "overfit|过学习", "模型在训练集表现好、在测试集泛化差的现象"),
    ("C17", "欠拟合", "机器学习概念", "Underfitting", "underfit|欠学习", "模型容量不足，训练集与测试集表现都差的现象"),
    ("C18", "正则化", "机器学习概念", "Regularization", "regularization|L1正则|L2正则|惩罚项", "在损失函数中加入惩罚项以抑制过拟合的方法"),
    ("C19", "梯度下降", "机器学习概念", "Gradient Descent", "GD|SGD|gradient descent|反向传播", "沿负梯度方向迭代更新参数的优化方法"),
    ("C20", "损失函数", "机器学习概念", "Loss Function", "loss|目标函数|代价函数", "衡量预测值与真实值差异、供优化器最小化的函数"),
    ("C21", "核技巧", "机器学习概念", "Kernel Trick", "kernel|高斯核|核函数", "用核函数隐式完成高维映射、避免显式计算内积的技巧"),
    ("C22", "特征工程", "机器学习概念", "Feature Engineering", "feature engineering|特征选择|特征构造", "构造、选择与变换特征以提升模型效果的过程"),
    ("C23", "交叉验证", "机器学习概念", "Cross Validation", "cross validation|K折|k-fold", "轮换划分训练集与验证集以稳定评估模型的方法"),
    ("C24", "偏差方差权衡", "机器学习概念", "Bias-Variance Tradeoff", "bias variance|偏差-方差|泛化误差分解", "模型误差在偏差与方差之间取得平衡的关系"),
    ("C25", "信息熵", "机器学习概念", "Information Entropy", "entropy|熵|信息增益", "度量随机变量不确定性的指标，是决策树划分的依据"),
    ("C26", "最大间隔", "机器学习概念", "Maximum Margin", "margin|间隔最大化", "通过最大化分类间隔提升泛化能力的原理"),
    ("C27", "Bagging", "机器学习概念", "Bagging", "bootstrap aggregating|自助采样", "自助采样并行训练多个基学习器再投票或平均的集成策略"),
    ("C28", "Boosting", "机器学习概念", "Boosting", "提升方法|boosting", "串行训练弱学习器并逐步修正残差的集成策略"),
    ("C29", "注意力机制", "机器学习概念", "Attention Mechanism", "attention|自注意力|self-attention", "按相关性动态分配权重的序列建模机制"),
    ("C30", "表示学习", "机器学习概念", "Representation Learning", "representation learning|特征表示", "自动学习数据有效特征表示的一类方法总称"),
    ("C31", "模型评估", "机器学习概念", "Model Evaluation", "evaluation|评估方法", "用评价指标与验证策略衡量模型性能的过程"),
    ("C32", "超参数调优", "机器学习概念", "Hyperparameter Tuning", "调参|网格搜索|贝叶斯优化", "搜索最优超参数组合以提升模型性能的过程"),
    ("C33", "数据不平衡", "机器学习概念", "Class Imbalance", "样本不平衡|正负样本不均衡|imbalance", "类别样本分布严重不均、导致模型偏向多数类的问题"),
    ("C34", "维度灾难", "机器学习概念", "Curse of Dimensionality", "curse of dimensionality|高维稀疏", "高维空间中样本稀疏、距离失效导致模型性能下降的现象"),
    ("C35", "序贯决策任务", "机器学习概念", "Sequential Decision Making", "序贯决策|决策任务|control", "在连续交互中依据长期回报选择动作的任务"),
    ("C36", "机器学习", "机器学习概念", "Machine Learning", "ML|machine learning|机器学习学科", "研究如何让计算机从数据中自动学习规律并持续改进性能的学科"),
    ("C37", "线性假设", "机器学习概念", "Linear Assumption", "线性关系假设|线性模型", "假设目标值是输入特征的线性组合、误差独立同分布的建模前提"),
    ("C38", "Sigmoid函数", "机器学习概念", "Sigmoid Function", "sigmoid|S型函数|对数几率函数|激活函数", "把任意实数压缩到0~1区间的S形函数，用于把线性输出转成概率"),

    # ---------------- 算法（34） ----------------
    ("A01", "线性回归", "算法", "Linear Regression", "linear regression|LR|最小二乘", "用线性函数拟合连续目标值的最小二乘回归算法"),
    ("A02", "逻辑回归", "算法", "Logistic Regression", "logistic regression|对数几率回归|逻辑斯蒂回归", "用Sigmoid函数把线性输出映射为类别概率的分类算法"),
    ("A03", "岭回归", "算法", "Ridge Regression", "ridge|L2正则线性回归", "在线性回归损失中加入L2正则项的回归算法"),
    ("A04", "Lasso回归", "算法", "Lasso Regression", "lasso|L1正则线性回归", "加入L1正则项、可产生稀疏特征的回归算法"),
    ("A05", "决策树", "算法", "Decision Tree", "decision tree|CART|ID3|C4.5", "依据信息增益等准则递归划分特征空间构建树结构的算法"),
    ("A06", "随机森林", "算法", "Random Forest", "random forest|RF", "用Bagging组合多棵决策树并投票或平均的集成算法"),
    ("A07", "梯度提升树", "算法", "GBDT", "GBDT|Gradient Boosting Decision Tree|GBM", "串行拟合损失负梯度的树集成算法"),
    ("A08", "XGBoost", "算法", "XGBoost", "xgboost|XGB", "引入正则项与二阶泰勒展开的高效梯度提升树实现"),
    ("A09", "LightGBM", "算法", "LightGBM", "lightgbm|lgb|lgbm", "采用直方图算法与叶子生长策略的轻量梯度提升树"),
    ("A10", "CatBoost", "算法", "CatBoost", "catboost", "原生支持类别特征、使用有序提升的梯度提升树"),
    ("A11", "支持向量机", "算法", "SVM", "SVM|support vector machine|支持向量", "最大化分类间隔、可借助核技巧处理非线性的分类算法"),
    ("A12", "K近邻", "算法", "KNN", "KNN|k-nearest neighbors|最近邻", "依据最近的K个样本投票或取平均的惰性学习算法"),
    ("A13", "朴素贝叶斯", "算法", "Naive Bayes", "naive bayes|NB|贝叶斯分类", "基于特征条件独立假设与贝叶斯定理的分类算法"),
    ("A14", "多层感知机", "算法", "MLP", "MLP|多层感知机|全连接网络|感知机", "由全连接层与非线性激活堆叠而成的基础神经网络"),
    ("A15", "卷积神经网络", "算法", "CNN", "CNN|卷积网络|convnet", "用卷积核提取局部空间特征的前馈深度网络"),
    ("A16", "循环神经网络", "算法", "RNN", "RNN|循环网络|recurrent", "通过循环单元共享参数处理变长序列的深度网络"),
    ("A17", "LSTM", "算法", "LSTM", "LSTM|长短期记忆网络|长短期记忆", "用输入门、遗忘门、输出门缓解梯度消失的循环网络"),
    ("A18", "GRU", "算法", "GRU", "GRU|门控循环单元", "把门控简化为更新门与重置门的循环网络"),
    ("A19", "Transformer", "算法", "Transformer", "transformer|自注意力网络", "完全基于自注意力与前馈网络的序列建模架构"),
    ("A20", "自编码器", "算法", "Autoencoder", "autoencoder|AE|自编码网络", "通过编码-解码重构输入来学习低维表示的无监督网络"),
    ("A21", "变分自编码器", "算法", "VAE", "VAE|variational autoencoder", "在潜空间引入概率分布约束的生成式自编码器"),
    ("A22", "生成对抗网络", "算法", "GAN", "GAN|generative adversarial network|对抗生成网络", "生成器与判别器对抗训练的生成式网络"),
    ("A23", "K均值聚类", "算法", "K-Means", "kmeans|K-means|K均值", "迭代更新簇中心以最小化簇内平方误差的聚类算法"),
    ("A24", "DBSCAN", "算法", "DBSCAN", "dbscan|密度聚类", "基于密度可达性发现任意形状簇的聚类算法"),
    ("A25", "层次聚类", "算法", "Hierarchical Clustering", "hierarchical clustering|凝聚聚类|AGNES", "通过不断合并或分裂簇形成聚类树的聚类算法"),
    ("A26", "主成分分析", "算法", "PCA", "PCA|主成分分析|principal component", "用正交变换提取最大方差方向实现线性降维的算法"),
    ("A27", "t-SNE", "算法", "t-SNE", "tsne|t-SNE|流形降维", "用概率分布匹配实现非线性降维与可视化的算法"),
    ("A28", "孤立森林", "算法", "Isolation Forest", "isolation forest|iForest", "用随机切分路径长度衡量样本异常程度的算法"),
    ("A29", "单类SVM", "算法", "One-Class SVM", "one-class svm|OCSVM|单类支持向量机", "仅用正常样本学习决策边界以检测异常的算法"),
    ("A30", "Q学习", "算法", "Q-Learning", "q-learning|Qlearning|时序差分", "通过时序差分迭代更新动作价值表的无模型强化学习算法"),
    ("A31", "深度Q网络", "算法", "DQN", "DQN|deep q network|深度Q学习", "用神经网络近似Q值并引入经验回放的深度强化学习算法"),
    ("A32", "策略梯度", "算法", "Policy Gradient", "policy gradient|REINFORCE|PG", "直接对策略参数做梯度上升以最大化期望回报的算法"),
    ("A33", "协同过滤", "算法", "Collaborative Filtering", "collaborative filtering|CF|协同过滤推荐", "基于用户-物品交互相似度做推荐的算法"),
    ("A34", "因子分解机", "算法", "Factorization Machine", "FM|factorization machine|因子分解机", "用隐向量内积建模特征二阶交互的算法"),

    # ---------------- 模型（23） ----------------
    ("M01", "ResNet-50图像分类模型", "模型", "ResNet-50", "resnet50|ResNet|残差网络|resnet-50", "用残差连接缓解深层网络退化问题的50层CNN分类模型"),
    ("M02", "VGG-16图像分类模型", "模型", "VGG-16", "vgg16|VGG|vgg-16", "用连续3x3小卷积堆叠加深网络的16层CNN分类模型"),
    ("M03", "EfficientNet-B0图像分类模型", "模型", "EfficientNet-B0", "efficientnet|EfficientNet-B0", "用复合缩放策略平衡深度、宽度与分辨率的轻量分类模型"),
    ("M04", "YOLOv5目标检测模型", "模型", "YOLOv5", "yolov5|YOLO|yolo v5", "单阶段回归式、可实时推理的目标检测模型"),
    ("M05", "Faster R-CNN目标检测模型", "模型", "Faster R-CNN", "faster rcnn|Faster R-CNN|faster-rcnn", "以RPN生成候选框、两阶段精修的目标检测模型"),
    ("M06", "U-Net图像分割模型", "模型", "U-Net", "unet|U-Net|图像分割", "编码-解码加跳跃连接的像素级图像分割模型"),
    ("M07", "BERT-base中文预训练模型", "模型", "BERT-base", "bert|BERT|bert-base|中文BERT", "基于双向Transformer编码器的中文预训练语言模型"),
    ("M08", "RoBERTa-wwm中文预训练模型", "模型", "RoBERTa-wwm", "roberta|RoBERTa|wwm", "采用全词掩码与更大语料训练的BERT改进模型"),
    ("M09", "GPT-2文本生成模型", "模型", "GPT-2", "gpt2|GPT-2|GPT", "基于Transformer解码器的自回归文本生成模型"),
    ("M10", "TextCNN文本分类模型", "模型", "TextCNN", "textcnn|TextCNN", "用多尺寸卷积核提取n-gram特征的文本分类模型"),
    ("M11", "BiLSTM-CRF命名实体识别模型", "模型", "BiLSTM-CRF", "bilstm-crf|BiLSTM|命名实体识别|NER", "双向LSTM编码加CRF标签约束的序列标注模型"),
    ("M12", "Word2Vec词向量模型", "模型", "Word2Vec", "word2vec|w2v|词向量", "用CBOW或Skip-gram从上下文学习词向量的浅层模型"),
    ("M13", "GloVe词向量模型", "模型", "GloVe", "glove|全局词向量", "基于全局共现矩阵分解学习词向量的模型"),
    ("M14", "FM点击率预估模型", "模型", "FM-CTR", "fm|FM|点击率预估", "用隐向量交叉建模二阶特征的点击率预估模型"),
    ("M15", "DeepFM点击率预估模型", "模型", "DeepFM", "deepfm|DeepFM", "FM与深度网络并联、同时建模低阶与高阶特征的点击率模型"),
    ("M16", "DIN推荐排序模型", "模型", "DIN", "din|DIN|深度兴趣网络", "用注意力机制建模用户历史行为兴趣的推荐排序模型"),
    ("M17", "矩阵分解推荐模型", "模型", "MF", "矩阵分解|MF|matrix factorization", "把用户-物品评分矩阵分解为隐向量的推荐模型"),
    ("M18", "XGBoost风控评分模型", "模型", "XGBoost-Risk", "风控模型|信用评分|XGBoost评分卡", "用XGBoost输出信贷样本违约概率的风控评分模型"),
    ("M19", "LightGBM销量预测模型", "模型", "LightGBM-Sales", "销量预测|销量模型", "用LightGBM回归门店与促销特征预测销量的模型"),
    ("M20", "随机森林房价预测模型", "模型", "RF-HousePrice", "房价预测|房价模型", "用随机森林回归房屋属性预测成交价格的模型"),
    ("M21", "逻辑回归信用违约模型", "模型", "LR-Credit", "违约预测|评分卡模型|信用违约", "用逻辑回归输出违约概率的可解释信用评分模型"),
    ("M22", "自编码器异常检测模型", "模型", "AE-Anomaly", "重构误差异常检测", "以重构误差作为异常分数的无监督异常检测模型"),
    ("M23", "LSTM股票价格预测模型", "模型", "LSTM-Stock", "股价预测|股票预测", "用LSTM建模历史行情序列预测下一交易日价格的模型"),

    # ---------------- 评价指标（18） ----------------
    ("P01", "准确率", "评价指标", "Accuracy", "accuracy|ACC|正确率", "预测正确的样本数占总样本数的比例"),
    ("P02", "精确率", "评价指标", "Precision", "precision|查准率", "预测为正的样本中真正为正的比例"),
    ("P03", "召回率", "评价指标", "Recall", "recall|查全率|灵敏度", "真正为正的样本中被成功预测出来的比例"),
    ("P04", "F1分数", "评价指标", "F1-Score", "f1|F1|f1-score|F值", "精确率与召回率的调和平均，兼顾二者平衡"),
    ("P05", "ROC-AUC", "评价指标", "ROC-AUC", "auc|AUC|roc_auc", "ROC曲线下面积，衡量模型对正负样本的排序区分能力"),
    ("P06", "PR-AUC", "评价指标", "PR-AUC", "prauc|PR曲线|平均精确率AP", "精确率-召回率曲线下面积，适合类别不平衡场景"),
    ("P07", "混淆矩阵", "评价指标", "Confusion Matrix", "confusion matrix|TP FP FN TN", "按TP/FP/FN/TN统计分类结果的表格化评估工具"),
    ("P08", "均方误差", "评价指标", "MSE", "mse|MSE|平方损失", "预测值与真实值之差的平方的均值"),
    ("P09", "均方根误差", "评价指标", "RMSE", "rmse|RMSE|根均方误差", "均方误差的平方根，量纲与目标变量一致"),
    ("P10", "平均绝对误差", "评价指标", "MAE", "mae|MAE|绝对误差", "预测误差绝对值的均值，对异常值更稳健"),
    ("P11", "决定系数", "评价指标", "R2", "r2|R2|R²|拟合优度", "回归模型所能解释的目标方差比例"),
    ("P12", "轮廓系数", "评价指标", "Silhouette Coefficient", "silhouette|轮廓系数|SIL", "同时衡量簇内紧密度与簇间分离度的聚类指标"),
    ("P13", "调整兰德指数", "评价指标", "Adjusted Rand Index", "ARI|rand index|兰德指数", "校正随机一致性后的聚类划分一致程度指标"),
    ("P14", "困惑度", "评价指标", "Perplexity", "perplexity|PPL|困惑度", "语言模型对序列预测不确定性的度量，越低越好"),
    ("P15", "BLEU", "评价指标", "BLEU", "bleu|BLEU-4|双语评估替补", "基于n-gram重合度衡量生成文本质量的指标"),
    ("P16", "平均精度均值", "评价指标", "mAP", "map|mAP|平均精度", "各类别平均精度AP的均值，是目标检测的核心指标"),
    ("P17", "交并比", "评价指标", "IoU", "iou|IoU|Jaccard", "预测框与真实框重叠面积与并集面积之比"),
    ("P18", "NDCG", "评价指标", "NDCG", "ndcg|NDCG@K|归一化折损累计增益", "考虑位置折扣的排序质量指标，常用于推荐与搜索"),
]

# ---------------------------------------------------------------------------
# 三、关系表
# 字段：(头实体id, 关系类型, 尾实体id)
# 建模约定（详见 docs/01_schema_design.md）：
#   属于   —— 概念→上位概念 / 算法→范式 / 模型→算法 / 指标→任务
#   衍生自 —— 算法或模型的"改进来源"，方向为「新→旧」
#   基于   —— 依赖的核心原理或方法，方向为「使用者→被依赖者」
#   可对比 —— 同层同类可横向对比，逻辑对称，数据中只存一条（symmetric=true）
#   应用于 —— 算法/模型→任务，方向为「方法→任务」
# ---------------------------------------------------------------------------
RELATIONS = [
    # ---------- 属于：四大范式 → 机器学习（课程大纲的第一层） ----------
    ("C01", "属于", "C36"), ("C02", "属于", "C36"),
    ("C03", "属于", "C36"), ("C05", "属于", "C36"),

    # ---------- 属于：任务/范式之间的层级 ----------
    ("C09", "属于", "C01"), ("C10", "属于", "C01"), ("C14", "属于", "C01"),
    ("C15", "属于", "C01"), ("C11", "属于", "C02"), ("C12", "属于", "C02"),
    ("C13", "属于", "C02"), ("C27", "属于", "C06"), ("C28", "属于", "C06"),
    ("C06", "属于", "C01"), ("C07", "属于", "C30"), ("C35", "属于", "C05"),
    ("C03", "属于", "C01"), ("C04", "属于", "C02"), ("C08", "属于", "C01"),
    ("C23", "属于", "C31"),

    # ---------- 属于：算法 → 学习范式 ----------
    ("A01", "属于", "C01"), ("A02", "属于", "C01"), ("A03", "属于", "C01"),
    ("A04", "属于", "C01"), ("A05", "属于", "C01"), ("A06", "属于", "C06"),
    ("A07", "属于", "C06"), ("A08", "属于", "C06"), ("A09", "属于", "C06"),
    ("A10", "属于", "C06"), ("A11", "属于", "C01"), ("A12", "属于", "C01"),
    ("A13", "属于", "C01"), ("A14", "属于", "C07"), ("A15", "属于", "C07"),
    ("A16", "属于", "C07"), ("A17", "属于", "C07"), ("A18", "属于", "C07"),
    ("A19", "属于", "C07"), ("A20", "属于", "C02"), ("A21", "属于", "C07"),
    ("A22", "属于", "C02"), ("A23", "属于", "C02"), ("A24", "属于", "C02"),
    ("A25", "属于", "C02"), ("A26", "属于", "C02"), ("A27", "属于", "C02"),
    ("A28", "属于", "C02"), ("A29", "属于", "C02"), ("A30", "属于", "C05"),
    ("A31", "属于", "C05"), ("A32", "属于", "C05"), ("A33", "属于", "C02"),
    ("A34", "属于", "C01"),

    # ---------- 属于：模型 → 基础算法 / 表示学习方法 ----------
    ("M01", "属于", "A15"), ("M02", "属于", "A15"), ("M03", "属于", "A15"),
    ("M04", "属于", "A15"), ("M05", "属于", "A15"), ("M06", "属于", "A15"),
    ("M07", "属于", "A19"), ("M08", "属于", "A19"), ("M09", "属于", "A19"),
    ("M10", "属于", "A15"), ("M11", "属于", "A17"), ("M12", "属于", "C30"),
    ("M13", "属于", "C30"), ("M14", "属于", "A34"), ("M17", "属于", "A33"),
    ("M18", "属于", "A08"), ("M19", "属于", "A09"), ("M20", "属于", "A06"),
    ("M21", "属于", "A02"), ("M22", "属于", "A20"), ("M23", "属于", "A17"),

    # ---------- 属于：评价指标 → 任务 ----------
    ("P01", "属于", "C09"), ("P02", "属于", "C09"), ("P03", "属于", "C09"),
    ("P04", "属于", "C09"), ("P05", "属于", "C09"), ("P06", "属于", "C09"),
    ("P07", "属于", "C09"), ("P16", "属于", "C09"), ("P17", "属于", "C09"),
    ("P08", "属于", "C10"), ("P09", "属于", "C10"), ("P10", "属于", "C10"),
    ("P11", "属于", "C10"), ("P12", "属于", "C11"), ("P13", "属于", "C11"),
    ("P14", "属于", "C15"), ("P15", "属于", "C15"), ("P18", "属于", "C14"),

    # ---------- 衍生自：算法/模型改进谱系 ----------
    ("A03", "衍生自", "A01"), ("A04", "衍生自", "A01"),
    ("A06", "衍生自", "A05"), ("A07", "衍生自", "A05"),
    ("A08", "衍生自", "A07"), ("A09", "衍生自", "A07"), ("A10", "衍生自", "A07"),
    ("A15", "衍生自", "A14"), ("A16", "衍生自", "A14"),
    ("A17", "衍生自", "A16"), ("A18", "衍生自", "A16"), ("A19", "衍生自", "A16"),
    ("A21", "衍生自", "A20"), ("A27", "衍生自", "A26"), ("A29", "衍生自", "A11"),
    ("A31", "衍生自", "A30"), ("M08", "衍生自", "M07"), ("M15", "衍生自", "M14"),
    ("M03", "衍生自", "M01"),
    # 决策树直接衍生出的四个树集成算法（大纲要求：决策树 → 随机森林/XGBoost/LightGBM/CatBoost）
    ("A08", "衍生自", "A05"), ("A09", "衍生自", "A05"), ("A10", "衍生自", "A05"),

    # ---------- 基于：核心原理依赖 ----------
    ("A01", "基于", "C19"), ("A02", "基于", "C19"), ("A02", "基于", "C20"),
    ("A05", "基于", "C25"), ("A06", "基于", "C27"), ("A07", "基于", "C28"),
    ("A08", "基于", "C28"), ("A11", "基于", "C26"), ("A11", "基于", "C21"),
    ("A14", "基于", "C19"), ("A15", "基于", "C19"), ("A16", "基于", "C19"),
    ("A19", "基于", "C29"), ("A20", "基于", "C20"), ("A22", "基于", "C20"),
    ("A23", "基于", "C20"), ("A32", "基于", "C19"), ("A34", "基于", "C20"),
    ("C07", "基于", "C19"), ("C18", "基于", "C20"), ("M16", "基于", "C29"),
    ("M11", "基于", "C20"), ("A28", "基于", "C34"),
    ("C31", "基于", "C24"), ("C32", "基于", "C23"), ("C16", "基于", "C24"),
    # 大纲点名的三个"原理级"依赖
    ("A01", "基于", "C37"), ("A02", "基于", "C38"), ("A06", "基于", "A05"),

    # ---------- 可对比：同级横向对比（对称关系，仅存一条） ----------
    ("A01", "可对比", "A03"), ("A01", "可对比", "A04"), ("A05", "可对比", "A12"),
    ("A05", "可对比", "A13"), ("A06", "可对比", "A07"), ("A08", "可对比", "A09"),
    ("A09", "可对比", "A10"), ("A08", "可对比", "A10"), ("A11", "可对比", "A12"),
    ("A12", "可对比", "A13"), ("A15", "可对比", "A16"), ("A17", "可对比", "A18"),
    ("A19", "可对比", "A16"), ("A20", "可对比", "A22"), ("A23", "可对比", "A24"),
    ("A24", "可对比", "A25"), ("A26", "可对比", "A27"), ("A28", "可对比", "A29"),
    ("A30", "可对比", "A32"), ("A02", "可对比", "A11"),
    ("M01", "可对比", "M02"), ("M02", "可对比", "M03"), ("M04", "可对比", "M05"),
    ("M07", "可对比", "M08"), ("M07", "可对比", "M09"), ("M12", "可对比", "M13"),
    ("M14", "可对比", "M15"), ("M18", "可对比", "M19"), ("M20", "可对比", "M21"),
    ("M22", "可对比", "M23"),
    ("P01", "可对比", "P02"), ("P02", "可对比", "P03"), ("P02", "可对比", "P04"),
    ("P05", "可对比", "P06"), ("P05", "可对比", "P01"), ("P08", "可对比", "P10"),
    ("P09", "可对比", "P10"), ("P11", "可对比", "P08"), ("P12", "可对比", "P13"),
    ("P16", "可对比", "P17"),
    ("C01", "可对比", "C02"), ("C16", "可对比", "C17"), ("C27", "可对比", "C28"),
    ("C09", "可对比", "C10"), ("C22", "可对比", "C30"), ("C33", "可对比", "C34"),
    ("C24", "可对比", "C34"),

    # ---------- 应用于：算法 → 任务 ----------
    ("A01", "应用于", "C10"), ("A02", "应用于", "C09"), ("A03", "应用于", "C10"),
    ("A04", "应用于", "C10"), ("A05", "应用于", "C09"), ("A05", "应用于", "C10"),
    ("A06", "应用于", "C09"), ("A06", "应用于", "C10"), ("A07", "应用于", "C09"),
    ("A07", "应用于", "C10"), ("A08", "应用于", "C09"), ("A08", "应用于", "C10"),
    ("A09", "应用于", "C09"), ("A09", "应用于", "C10"), ("A10", "应用于", "C09"),
    ("A11", "应用于", "C09"), ("A12", "应用于", "C09"), ("A12", "应用于", "C10"),
    ("A13", "应用于", "C09"), ("A14", "应用于", "C09"), ("A14", "应用于", "C10"),
    ("A15", "应用于", "C09"), ("A16", "应用于", "C15"), ("A17", "应用于", "C15"),
    ("A18", "应用于", "C15"), ("A19", "应用于", "C15"), ("A19", "应用于", "C09"),
    ("A20", "应用于", "C13"), ("A21", "应用于", "C15"), ("A22", "应用于", "C15"),
    ("A23", "应用于", "C11"), ("A24", "应用于", "C11"), ("A25", "应用于", "C11"),
    ("A26", "应用于", "C12"), ("A27", "应用于", "C12"), ("A28", "应用于", "C13"),
    ("A29", "应用于", "C13"), ("A30", "应用于", "C35"), ("A31", "应用于", "C35"),
    ("A32", "应用于", "C35"), ("A33", "应用于", "C14"), ("A34", "应用于", "C09"),

    # ---------- 应用于：模型 → 任务 ----------
    ("M01", "应用于", "C09"), ("M02", "应用于", "C09"), ("M03", "应用于", "C09"),
    ("M04", "应用于", "C09"), ("M05", "应用于", "C09"), ("M06", "应用于", "C09"),
    ("M07", "应用于", "C09"), ("M07", "应用于", "C15"), ("M08", "应用于", "C09"),
    ("M09", "应用于", "C15"), ("M10", "应用于", "C09"), ("M11", "应用于", "C09"),
    ("M12", "应用于", "C09"), ("M13", "应用于", "C09"), ("M14", "应用于", "C09"),
    ("M15", "应用于", "C09"), ("M16", "应用于", "C14"), ("M17", "应用于", "C14"),
    ("M18", "应用于", "C09"), ("M19", "应用于", "C10"), ("M20", "应用于", "C10"),
    ("M21", "应用于", "C09"), ("M22", "应用于", "C13"), ("M23", "应用于", "C10"),
]

# 对称关系：数据中只保留一条，查询与前端按无向处理
SYMMETRIC_RELATIONS = {"可对比"}


# ---------------------------------------------------------------------------
# 四、抽取置信度标注（产品优化点 2）
# ---------------------------------------------------------------------------
# 背景：知识图谱的"三元组是否正确"和"三元组有多可信"是两件事。
#       本项目的实体/关系由「规则抽取 + 人工结构化」得到，不同来源的证据强度不同：
#       课程大纲原文写死的（"决策树衍生随机森林"）当然比 Agent 依常识补的
#       （"随机森林可对比 K均值聚类"）更可信。给每条边打一个 0~1 的置信度，
#       前端就能用线宽/透明度和"只看高置信"过滤，答辩时也能主动交底"哪些是推断"。
#
# 置信度模型（4 档，简单可解释，不做概率校准）：
#   0.98  课程大纲原文 —— 语料里直接写了这句话，可逐字对照
#   0.92  领域通用定义 —— 两端都在语料实体表内，属于教材级别的标准归属
#   0.86  Agent 结构补全 —— 至少一端是扩展实体，但属于/应用于这类归属关系可机械判定
#   0.78  Agent 推理补全 —— 基于/可对比/衍生自这类需要语义判断的关系，建议人工复核
# ---------------------------------------------------------------------------

# 语料核心实体：课程大纲原文点名的知识点（抽取置信度 1.00，其余为 0.90 的扩展实体）
SYLLABUS_CORE = {
    "机器学习", "监督学习", "无监督学习", "半监督学习", "强化学习",
    "分类任务", "回归任务", "聚类任务", "降维任务",
    "线性回归", "岭回归", "Lasso回归", "逻辑回归", "支持向量机", "决策树",
    "随机森林", "XGBoost", "LightGBM", "CatBoost", "K均值聚类", "主成分分析",
    "均方误差", "准确率", "精确率", "召回率", "F1分数", "轮廓系数",
    "线性假设", "Sigmoid函数", "最大间隔",
}

# 课程大纲原文直接陈述的三元组（按名字书写，便于与大纲逐条对照；生成时校验必须存在）
OUTLINE_TRIPLES = {
    ("监督学习", "属于", "机器学习"), ("无监督学习", "属于", "机器学习"),
    ("半监督学习", "属于", "机器学习"), ("强化学习", "属于", "机器学习"),
    ("分类任务", "属于", "监督学习"), ("回归任务", "属于", "监督学习"),
    ("聚类任务", "属于", "无监督学习"), ("降维任务", "属于", "无监督学习"),
    ("线性回归", "基于", "线性假设"), ("线性回归", "应用于", "回归任务"),
    ("岭回归", "衍生自", "线性回归"), ("Lasso回归", "衍生自", "线性回归"),
    ("逻辑回归", "基于", "Sigmoid函数"), ("逻辑回归", "可对比", "支持向量机"),
    ("逻辑回归", "应用于", "分类任务"),
    ("决策树", "应用于", "分类任务"), ("决策树", "应用于", "回归任务"),
    ("随机森林", "衍生自", "决策树"), ("随机森林", "基于", "决策树"),
    ("XGBoost", "衍生自", "决策树"), ("LightGBM", "衍生自", "决策树"),
    ("CatBoost", "衍生自", "决策树"),
    ("支持向量机", "基于", "最大间隔"),
    ("K均值聚类", "属于", "无监督学习"), ("K均值聚类", "应用于", "聚类任务"),
    ("主成分分析", "属于", "无监督学习"), ("主成分分析", "应用于", "降维任务"),
    ("均方误差", "属于", "回归任务"),
    ("准确率", "属于", "分类任务"), ("精确率", "属于", "分类任务"),
    ("召回率", "属于", "分类任务"), ("F1分数", "属于", "分类任务"),
    ("轮廓系数", "属于", "聚类任务"),
}

# 需要人工复核的置信度阈值：低于它的边在 data/ 里单独导出成清单
CONFIDENCE_REVIEW_THRESHOLD = 0.80

# 关系类型 → 语义判断难度（决定"非大纲边"的默认置信度）
_MECHANICAL_RELATIONS = {"属于", "应用于"}      # 归属/用途，可机械判定
_INFERRED_RELATIONS = {"基于", "可对比", "衍生自"}  # 需要语义理解，误判概率更高


def relation_confidence(head_name, rel, tail_name):
    """
    给一条三元组打置信度，返回 (confidence, evidence)。
    规则很短，就是为了让老师一眼看懂、让结果可复现——不做模型打分。
    """
    if (head_name, rel, tail_name) in OUTLINE_TRIPLES or (tail_name, rel, head_name) in OUTLINE_TRIPLES:
        return 0.98, "课程大纲原文"
    if head_name in SYLLABUS_CORE and tail_name in SYLLABUS_CORE:
        return 0.92, "领域通用定义"
    if rel in _MECHANICAL_RELATIONS:
        return 0.86, "Agent 结构补全"
    return 0.78, "Agent 推理补全"


def entity_confidence(name):
    """实体置信度：语料原文点名 1.00；Agent 按领域常识扩充 0.90。"""
    return (1.00, "课程语料") if name in SYLLABUS_CORE else (0.90, "Agent 扩展")


# ---------------------------------------------------------------------------
# 五、关系描述生成规则（课程设计不需要人工写 200 条描述，用模板保证一致性）
# ---------------------------------------------------------------------------
def relation_description(head_name, rel, tail_name):
    """把三元组转成一句人话，便于前端悬浮提示与答辩讲解。"""
    templates = {
        "属于": "{h}属于{t}",
        "衍生自": "{h}由{t}改进衍生而来",
        "基于": "{h}的实现依赖{t}",
        "可对比": "{h}与{t}常被放在一起横向对比",
        "应用于": "{h}可应用于{t}",
    }
    return templates[rel].format(h=head_name, t=tail_name)


def _fail(msg):
    print("[ERROR] " + msg)
    sys.exit(1)


def check_integrity(entities, relations):
    """导出前自检：类型合法性、悬空边、自环、重复边、关系类型越界。"""
    id2entity = {e[0]: e for e in entities}
    if len(id2entity) != len(entities):
        _fail("存在重复的实体 id")

    for e in entities:
        if e[2] not in ENTITY_TYPES:
            _fail("实体 %s 的类型 %s 不在允许的 4 类内" % (e[0], e[2]))
        if not e[5]:
            _fail("实体 %s 缺少描述" % e[0])

    seen = set()
    pair_set = set()
    for head, rel, tail in relations:
        if rel not in RELATION_TYPES:
            _fail("关系类型 %s 不在允许的 5 类内" % rel)
        if head not in id2entity or tail not in id2entity:
            _fail("悬空关系：%s -%s-> %s" % (head, rel, tail))
        if head == tail:
            _fail("出现自环：%s" % head)
        key = (head, rel, tail)
        if key in seen:
            _fail("重复关系：%s" % (key,))
        seen.add(key)
        if rel in SYMMETRIC_RELATIONS:
            reverse = (tail, rel, head)
            if reverse in pair_set:
                _fail("对称关系重复登记：%s / %s" % (key, reverse))
            pair_set.add(key)
        # 关系语义约束：可对比不允许跨层（模型 vs 指标这类对比视为错误数据）
        ht, tt = id2entity[head][2], id2entity[tail][2]
        if rel == "可对比" and ht != tt:
            _fail("可对比关系两端类型不一致：%s(%s) - %s(%s)" % (head, ht, tail, tt))
        if rel == "应用于" and tt != "机器学习概念":
            _fail("应用于关系的尾实体必须是机器学习概念：%s" % (key,))
        if rel == "应用于" and ht not in ("算法", "模型"):
            _fail("应用于关系的头实体必须是算法或模型：%s" % (key,))

    # 置信度标注的前提：OUTLINE_TRIPLES 里写的每一句大纲原文都必须在关系表里真实存在，
    # 否则说明"大纲覆盖"有漏项，或者是名字写错了（比如写成"K-Means"而实体叫"K均值聚类"）。
    name_set = {e[1] for e in entities}
    rel_name_set = {(id2entity[h][1], r, id2entity[t][1]) for h, r, t in relations}
    for h, r, t in OUTLINE_TRIPLES:
        for nm in (h, t):
            if nm not in name_set:
                _fail("OUTLINE_TRIPLES 引用了不存在的实体：%s" % nm)
        if (h, r, t) not in rel_name_set and (t, r, h) not in rel_name_set:
            _fail("OUTLINE_TRIPLES 里的大纲原文在关系表中缺失：%s -%s-> %s" % (h, r, t))


def build():
    check_integrity(ENTITIES, RELATIONS)

    id2entity = {e[0]: e for e in ENTITIES}

    # ---------- 1. 实体记录 ----------
    entity_rows = []
    for eid, name, etype, en, alias, desc in ENTITIES:
        conf, source = entity_confidence(name)
        entity_rows.append({
            "id": eid,
            "name": name,
            "type": etype,
            "label": TYPE_TO_LABEL[etype],
            "en_name": en,
            "aliases": alias,
            "description": desc,
            "color": TYPE_COLOR[etype],
            "confidence": "%.2f" % conf,   # 抽取置信度（优化点：可追溯、可过滤）
            "source": source,             # 证据来源：课程语料 / Agent 扩展
        })

    # ---------- 2. 关系记录 ----------
    relation_rows = []
    for idx, (head, rel, tail) in enumerate(RELATIONS, start=1):
        h, t = id2entity[head], id2entity[tail]
        conf, evidence = relation_confidence(h[1], rel, t[1])
        relation_rows.append({
            "id": "R%03d" % idx,
            "head_id": h[0],
            "head_name": h[1],
            "head_type": h[2],
            "relation": rel,
            "tail_id": t[0],
            "tail_name": t[1],
            "tail_type": t[2],
            "symmetric": "true" if rel in SYMMETRIC_RELATIONS else "false",
            "description": relation_description(h[1], rel, t[1]),
            "confidence": "%.2f" % conf,
            "evidence": evidence,
        })

    os.makedirs(DATA_DIR, exist_ok=True)

    # ---------- 3. 写 CSV（utf-8 无 BOM，Neo4j LOAD CSV 可直接读取） ----------
    entity_fields = ["id", "name", "type", "label", "en_name", "aliases", "description", "color",
                     "confidence", "source"]
    with open(os.path.join(DATA_DIR, "entities.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=entity_fields)
        writer.writeheader()
        writer.writerows(entity_rows)

    rel_fields = ["id", "head_id", "head_name", "head_type", "relation",
                  "tail_id", "tail_name", "tail_type", "symmetric", "description",
                  "confidence", "evidence"]
    with open(os.path.join(DATA_DIR, "relations.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rel_fields)
        writer.writeheader()
        writer.writerows(relation_rows)

    # ---------- 4. 写课程交付要求的"三元组表" entity_relation.csv ----------
    # 字段严格按课程要求：head / relation / tail，一行一个三元组，共 257 行。
    # 实体类型与别名不在这张表里（避免字段超纲），由 data/entities.csv 按 name 关联。
    triple_path = os.path.join(DATA_DIR, "entity_relation.csv")
    with open(triple_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["head", "relation", "tail"])
        for head, rel, tail in RELATIONS:
            writer.writerow([id2entity[head][1], rel, id2entity[tail][1]])

    # ---------- 4.1 低置信度关系清单（人工复核用的"待办"） ----------
    # 只导出 conf < review_threshold 的边，答辩时可以直接说：
    # "这 %d 条是 Agent 推断出来的，我列了清单逐条复核，其余 %d 条都有原文或教材依据。"
    review_rows = [r for r in relation_rows if float(r["confidence"]) < CONFIDENCE_REVIEW_THRESHOLD]
    with open(os.path.join(DATA_DIR, "low_confidence_relations.csv"), "w",
              newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "head_name", "relation", "tail_name",
                                               "confidence", "evidence", "description"])
        writer.writeheader()
        for r in review_rows:
            writer.writerow({k: r[k] for k in writer.fieldnames})

    # ---------- 5. 写 kg.json（后端 / 前端直接读取的聚合数据） ----------
    kg = {
        "meta": {
            "name": "机器学习领域知识图谱",
            "version": "1.0.0",
            "entity_types": ENTITY_TYPES,
            "relation_types": RELATION_TYPES,
            "type_to_label": TYPE_TO_LABEL,
            "type_color": TYPE_COLOR,
            "counts": {
                "entities": len(entity_rows),
                "relations": len(relation_rows),
            },
            # 置信度模型（前端图例、后端低置信提示都用这份元信息，避免两边写死）
            "confidence_model": {
                "levels": [
                    {"score": 0.98, "evidence": "课程大纲原文"},
                    {"score": 0.92, "evidence": "领域通用定义"},
                    {"score": 0.86, "evidence": "Agent 结构补全"},
                    {"score": 0.78, "evidence": "Agent 推理补全"},
                ],
                "review_threshold": CONFIDENCE_REVIEW_THRESHOLD,
                "entity_levels": [
                    {"score": 1.00, "source": "课程语料"},
                    {"score": 0.90, "source": "Agent 扩展"},
                ],
            },
        },
        "entities": entity_rows,
        "relations": [
            {
                "id": r["id"],
                "source": r["head_id"],
                "target": r["tail_id"],
                "type": r["relation"],
                "symmetric": r["symmetric"] == "true",
                "description": r["description"],
                "confidence": float(r["confidence"]),
                "evidence": r["evidence"],
            }
            for r in relation_rows
        ],
    }
    with open(os.path.join(DATA_DIR, "kg.json"), "w", encoding="utf-8") as f:
        json.dump(kg, f, ensure_ascii=False, indent=2)

    # ---------- 6. 控制台输出统计，便于答辩时直接引用 ----------
    type_counter = Counter(e[2] for e in ENTITIES)
    rel_counter = Counter(r[1] for r in RELATIONS)
    print("=" * 56)
    print("数据集生成完成")
    print("-" * 56)
    print("实体总数：%d（目标 80~120）" % len(ENTITIES))
    for t in ENTITY_TYPES:
        print("  - %-8s %3d" % (t, type_counter.get(t, 0)))
    print("关系总数（三元组）：%d" % len(RELATIONS))
    for r in RELATION_TYPES:
        print("  - %-4s %3d" % (r, rel_counter.get(r, 0)))
    print("-" * 56)
    conf_counter = Counter(r["confidence"] for r in relation_rows)
    print("关系置信度分布：")
    for score in sorted(conf_counter, reverse=True):
        ev = next(r["evidence"] for r in relation_rows if r["confidence"] == score)
        print("  - %s（%-12s）%3d" % (score, ev, conf_counter[score]))
    print("  - 待复核（< %.2f）：%d 条 → data/low_confidence_relations.csv"
          % (CONFIDENCE_REVIEW_THRESHOLD, len(review_rows)))
    print("-" * 56)
    print("输出：data/entity_relation.csv（head,relation,tail）")
    print("      data/entities.csv, data/relations.csv, data/kg.json")
    print("      data/low_confidence_relations.csv（低置信度复核清单）")
    print("=" * 56)


if __name__ == "__main__":
    build()
