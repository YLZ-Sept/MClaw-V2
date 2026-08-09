"""
Knowledge Base 搜索工具定义

包含知识库相关工具：
- knowledge_search: 混合检索知识库文档（BM25 + 向量 + RRF 融合）
"""

KNOWLEDGE_TOOLS = [
    {
        "name": "knowledge_search",
        "category": "Search",
        "description": (
            "搜索知识库中的文档内容。使用混合检索（关键词 + 语义），"
            "返回最相关的文档片段。\n\n"
            "使用场景：\n"
            "- 查找公司制度、产品文档、技术规范等已入库的知识\n"
            "- 回答需要引用内部文档的问题\n"
            "- 获取上传到知识库的 PDF、Word、Excel、Markdown 等文件内容\n\n"
            "参数说明：\n"
            "- query: 搜索查询，建议用自然语言描述你要找的内容\n"
            "- collection_id: 可选，限定在某个知识集合中搜索（不传则搜索所有绑定的集合）\n"
            "- top_k: 返回结果数（1-20，默认 5）"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询，用自然语言描述你要查找的内容",
                },
                "collection_id": {
                    "type": "string",
                    "description": "限定搜索的知识集合 ID（可选，不传则搜索所有可访问集合）",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果数量（1-20，默认 5）",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
]
