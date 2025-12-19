"""
监控指标搜索模块 - 提供 ZStack 监控指标的搜索功能
"""

import json
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MetricInfo:
    """监控指标信息"""
    name: str
    description: str
    namespace: str
    label_names: list[str]
    driver: str
    
    # 搜索用的 tokens
    tokens: list[str] = field(default_factory=list)


class MetricSearchIndex:
    """监控指标搜索索引"""
    
    def __init__(self):
        self.metrics: dict[str, MetricInfo] = {}  # name -> MetricInfo
        self.inverted_index: dict[str, set[str]] = {}  # token -> set of metric names
        self.namespaces: set[str] = set()
    
    def load_from_file(self, file_path: str | Path) -> None:
        """从 JSON 文件加载监控指标元数据并构建索引"""
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        metrics_list = data.get('metrics', [])
        
        for metric in metrics_list:
            name = metric.get('name', '')
            if not name:
                continue
            
            metric_info = MetricInfo(
                name=name,
                description=metric.get('description', ''),
                namespace=metric.get('namespace', ''),
                label_names=metric.get('labelNames', []),
                driver=metric.get('driver', ''),
            )
            
            # 驼峰拆分建立 tokens
            metric_info.tokens = self._split_camel_case(name)
            
            self.metrics[name] = metric_info
            self.namespaces.add(metric_info.namespace)
            
            # 建立倒排索引
            for token in metric_info.tokens:
                if token not in self.inverted_index:
                    self.inverted_index[token] = set()
                self.inverted_index[token].add(name)
    
    def _split_camel_case(self, name: str) -> list[str]:
        """
        驼峰拆分
        如 CPUUsedUtilization -> ["cpu", "used", "utilization"]
        """
        # 处理连续大写字母的情况，如 CPU -> cpu
        # 在大写字母后面跟小写字母前插入空格
        name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', name)
        # 在小写字母后面跟大写字母前插入空格
        name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
        words = name.split()
        return [w.lower() for w in words if w]
    
    def search(
        self,
        keywords: list[str],
        namespace: Optional[str] = None,
        limit: int = 20
    ) -> list[dict]:
        """
        搜索监控指标
        
        Args:
            keywords: 搜索关键词列表
            namespace: 可选的命名空间过滤
            limit: 最多返回数量
            
        Returns:
            匹配的监控指标列表
        """
        if not keywords:
            return []
        
        # 将关键词转为小写
        keywords_lower = [kw.lower() for kw in keywords]
        
        # 找出包含所有关键词的指标（交集）
        matched_metrics: Optional[set[str]] = None
        
        for kw in keywords_lower:
            # 查找包含该关键词的所有指标
            matching_names: set[str] = set()
            
            # 1. 首先检查是否直接匹配指标名称
            for metric_name in self.metrics.keys():
                if kw in metric_name.lower():
                    matching_names.add(metric_name)
            
            # 2. 然后检查倒排索引中的 token
            for token, names in self.inverted_index.items():
                # 支持部分匹配
                if token.startswith(kw) or kw in token:
                    matching_names.update(names)
            
            if matched_metrics is None:
                matched_metrics = matching_names
            else:
                matched_metrics = matched_metrics.intersection(matching_names)
        
        if not matched_metrics:
            return []
        
        # 按命名空间过滤
        if namespace:
            namespace_lower = namespace.lower()
            matched_metrics = {
                name for name in matched_metrics
                if self.metrics[name].namespace.lower() == namespace_lower
            }
        
        # 计算匹配分数并排序
        scored_results: list[tuple[str, float]] = []
        for name in matched_metrics:
            metric = self.metrics[name]
            score = self._calculate_score(metric, keywords_lower)
            scored_results.append((name, score))
        
        # 按分数降序排序
        scored_results.sort(key=lambda x: x[1], reverse=True)
        
        # 返回结果
        results = []
        for name, score in scored_results[:limit]:
            metric = self.metrics[name]
            results.append({
                'name': metric.name,
                'description': metric.description,
                'namespace': metric.namespace,
                'labelNames': metric.label_names,
            })
        
        return results
    
    def _calculate_score(self, metric: MetricInfo, keywords: list[str]) -> float:
        """计算指标与关键词的匹配分数"""
        score = 0.0
        metric_name_lower = metric.name.lower()
        
        for kw in keywords:
            # 检查指标名称是否完全等于关键词
            if metric_name_lower == kw:
                score += 100.0
            # 检查指标名称是否包含关键词
            elif kw in metric_name_lower:
                score += 10.0
            
            for token in metric.tokens:
                if token == kw:
                    score += 2.0
                elif token.startswith(kw):
                    score += 1.5
                elif kw in token:
                    score += 1.0
            
            if kw in metric.description.lower():
                score += 0.5
        
        # 减少冗长名称的分数
        score -= len(metric.tokens) * 0.1
        
        return score
    
    def get_metric(self, name: str) -> Optional[MetricInfo]:
        """获取指定指标的信息"""
        return self.metrics.get(name)
    
    def list_namespaces(self) -> list[str]:
        """列出所有命名空间"""
        return sorted(list(self.namespaces))

