"""
AI 回复生成器（抖音评论）
- 基于评论内容生成亲切接地气的回复
- 敏感话题检测与过滤（价格/竞品/政治/辱骂）
- 价格类问题引导私信，绝不直接报价
- 竞品问题不踩不捧，转回自身优势

与 E:\桌面\抖音\reply_generator.py 同源，新增:
- 输出 JSON 行格式（供 Agent 工具链解析）
- 批量生成入口，支持从 stdin 读 JSON
"""

import re
import sys
import json
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field


@dataclass
class ReplyResult:
    """回复结果"""
    reply: str                    # 生成的回复文案
    is_sensitive: bool = False    # 是否敏感（需人工确认）
    sensitive_type: str = ""      # 敏感类型
    suggestion: str = ""          # 处理建议
    confidence: float = 0.8       # 置信度


# 敏感词分类
SENSITIVE_PATTERNS = {
    "price": [
        r"多少钱", r"价格", r"报价", r"费用", r"怎么收费", r"收费标准",
        r"贵不贵", r"便宜", r"价位", r"要花多少", r"预算", r"几钱",
        r"多少米", r"多少银子", r"怎么卖", r"多少钱一套", r"多少钱一年",
    ],
    "competitor": [
        r"奇安信", r"深信服", r"启明星辰", r"绿盟", r"天融信",
        r"安恒", r"山石网科", r"美亚柏科", r"任子行", r"卫士通",
        r"和XX比", r"跟.*比", r"对比.*哪个", r"哪家好", r"有没有比.*好",
    ],
    "political": [
        r"共产党", r"习近平", r"政府", r"政治", r"民主", r"自由",
        r"六四", r"天安门", r"法轮功", r"台独", r"港独", r"藏独",
        r"反动", r"颠覆", r"敏感词", r"敏感话题",
    ],
    "abuse": [
        r"傻逼", r"傻B", r"草泥马", r"操你", r"妈的", r"垃圾",
        r"滚蛋", r"骗子", r"忽悠", r"传销", r"割韭菜",
    ],
}

# 业务关键词（米贝科技业务线）
BIZ_KEYWORDS = {
    "等保测评": ["等保", "等级保护", "等保测评", "等保2.0", "等保二级", "等保三级"],
    "数据安全": ["数据安全", "数据治理", "数据泄露", "数据合规", "个人信息保护"],
    "勒索病毒": ["勒索", "病毒", "挖矿", "被黑", "黑客", "攻击", "中毒"],
    "安全运维": ["安全运维", "运维服务", "安全服务", "应急响应", "驻场"],
    "CCRC认证": ["CCRC", "ccrc", "信息安全服务资质", "资质认证"],
    "MClaw智能体": ["MClaw", "mclaw", "智能体", "AI Agent", "AI智能体", "AI办公",
                   "企业智能体", "AI助手", "自动化", "AI客服", "AI运营"],
    "合作咨询": ["合作", "加盟", "代理", "商务", "怎么合作", "联系方式",
                "加微信", "留个电话", "联系电话", "怎么联系"],
}

# 回复模板库（亲切接地气风格，不超过 50 字）
REPLY_TEMPLATES = {
    "等保测评": {
        "positive": "做的呀～我们等保2.0从测评到整改全流程都有，需要的话可以私信聊聊你的具体情况 😊",
        "curious": "等保2.0我们很熟啦，从二级到三级都做过不少案例，你们是什么行业呀？",
        "price": "做的呀～等保测评的费用是根据等级和系统数量来定的，可以私信说说你的情况，我给你个参考 😊",
    },
    "数据安全": {
        "positive": "数据安全这块我们确实专业，从治理到防护一套都有，你是遇到什么具体问题了吗？",
        "curious": "数据安全是现在企业的头等大事啊，你们现在主要担心哪方面？",
    },
    "勒索病毒": {
        "positive": "勒索病毒太可怕了，我们有专门的防护方案，先备份后防护，双保险！需要方案的话可以私信 💪",
        "urgent": "如果已经中招了赶紧联系我们，应急响应团队24小时在线！",
    },
    "安全运维": {
        "positive": "安全运维服务我们有，按月按年都可以，相当于请了个专属安全团队，比招人划算多了 😄",
    },
    "CCRC认证": {
        "positive": "CCRC认证咨询我们也做哦，从准备到拿证全流程辅导，有需要可以私信了解详情～",
    },
    "MClaw智能体": {
        "positive": "MClaw是我们自研的企业智能体平台，客服、运营、数据分析都能做，一个智能体顶半个员工！想体验的话可以私信 🚀",
        "curious": "智能体现在真的能帮企业省不少人力，你们主要想解决什么场景的问题？",
        "price": "MClaw的方案是按场景定制的，有轻量版也有企业版，可以私信说说你的需求，我给你推荐最合适的 😊",
    },
    "合作咨询": {
        "positive": "好的呀～合作的事私信详聊，你是什么行业的？我先给你发点资料看看 📩",
    },
    "general": {
        "thanks": "感谢支持～有什么问题随时问我 😊",
        "curious": "这个问题问得好！你们是做什么行业的？我给你更具体的建议",
        "interesting": "哈哈有眼光！感兴趣的话可以多聊聊，我们做这行好多年了 😄",
    },
    "negative": {
        "price_redirect": "具体方案和价格可以私信聊聊，我们都是根据实际需求来定的，不会乱报价 😊",
    }
}


class ReplyGenerator:
    """抖音评论回复生成器"""

    def __init__(self, style: str = "亲切接地气"):
        self.style = style

    def detect_sensitive(self, text: str) -> Tuple[bool, str]:
        """检测评论是否包含敏感内容"""
        for stype, patterns in SENSITIVE_PATTERNS.items():
            for pat in patterns:
                if re.search(pat, text, re.IGNORECASE):
                    return True, stype
        return False, ""

    def detect_topic(self, text: str) -> List[str]:
        """检测评论涉及的业务主题"""
        topics = []
        for topic, keywords in BIZ_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in text.lower():
                    topics.append(topic)
                    break
        return topics

    def detect_intent(self, text: str) -> str:
        """判断评论意图"""
        text = text.strip()

        # 正向评价
        if re.search(r"(不错|挺好|厉害了|666|赞|支持|学到了|有用|收藏)", text):
            return "positive"

        # 提问/咨询
        if re.search(r"(吗|呢|怎么|如何|什么|多少|有没有|请问|请教)", text):
            return "question"

        # 需求/合作
        if re.search(r"(合作|联系|微信|电话|加个|私信|聊聊|咨询|了解一下)", text):
            return "consult"

        # 单纯感叹/互动
        if len(text) < 10 and re.search(r"(哈哈|哈哈哈|👍|🔥|💪|😊)", text):
            return "emotion"

        return "other"

    def generate_reply(self, comment_text: str, comment_user: str = "",
                       video_topic: str = "") -> ReplyResult:
        """
        生成回复
        Args:
            comment_text: 评论内容
            comment_user: 评论用户名
            video_topic: 视频主题
        Returns:
            ReplyResult 对象
        """
        # 1. 敏感词检测
        is_sensitive, sensitive_type = self.detect_sensitive(comment_text)

        # 2. 主题检测
        topics = self.detect_topic(comment_text)
        if not topics and video_topic:
            topics = self.detect_topic(video_topic)
        main_topic = topics[0] if topics else "general"

        # 3. 意图检测
        intent = self.detect_intent(comment_text)

        # 4. 根据敏感类型生成回复
        if is_sensitive:
            if sensitive_type == "price":
                # 价格类：引导私信，不直接报价
                if main_topic in REPLY_TEMPLATES and "price" in REPLY_TEMPLATES[main_topic]:
                    reply = REPLY_TEMPLATES[main_topic]["price"]
                else:
                    reply = REPLY_TEMPLATES["negative"]["price_redirect"]
                return ReplyResult(
                    reply=reply,
                    is_sensitive=True,
                    sensitive_type="price",
                    suggestion="价格类问题，已引导私信，建议人工确认后发送",
                    confidence=0.9,
                )

            elif sensitive_type == "competitor":
                # 竞品：不踩不捧，转回自身优势
                reply = "各家有各家的特点～我们主打落地效果和本地化服务，云南本地的客户响应特别快，有兴趣可以聊聊 😊"
                return ReplyResult(
                    reply=reply,
                    is_sensitive=True,
                    sensitive_type="competitor",
                    suggestion="竞品相关问题，回复已回避直接对比，建议人工确认",
                    confidence=0.85,
                )

            elif sensitive_type == "political":
                # 政治敏感：不回复
                return ReplyResult(
                    reply="",
                    is_sensitive=True,
                    sensitive_type="political",
                    suggestion="涉及敏感话题，建议直接跳过，不回复",
                    confidence=1.0,
                )

            elif sensitive_type == "abuse":
                # 辱骂：不回复
                return ReplyResult(
                    reply="",
                    is_sensitive=True,
                    sensitive_type="abuse",
                    suggestion="恶意/辱骂评论，建议直接跳过或删除",
                    confidence=1.0,
                )

        # 5. 非敏感：正常生成回复
        if main_topic in REPLY_TEMPLATES:
            tpls = REPLY_TEMPLATES[main_topic]
            if intent == "positive" and "positive" in tpls:
                reply = tpls["positive"]
            elif intent == "question" and "curious" in tpls:
                reply = tpls["curious"]
            elif intent == "consult":
                if "positive" in tpls:
                    reply = tpls["positive"]
                else:
                    reply = tpls.get("positive", REPLY_TEMPLATES["general"]["thanks"])
            else:
                reply = tpls.get("positive", REPLY_TEMPLATES["general"]["thanks"])
        else:
            reply = REPLY_TEMPLATES["general"]["thanks"]

        # 6. 如果是互动型评论，加个性化
        if intent == "emotion":
            reply = "哈哈谢谢支持～有什么想了解的随时问我 😄"

        return ReplyResult(
            reply=reply,
            is_sensitive=False,
            sensitive_type="",
            suggestion="",
            confidence=0.8,
        )

    def batch_generate(self, comments: List[Dict], video_topic: str = "") -> List[Dict]:
        """批量生成回复"""
        results = []
        for c in comments:
            result = self.generate_reply(
                comment_text=c.get("text", ""),
                comment_user=c.get("user", ""),
                video_topic=video_topic,
            )
            c["reply_result"] = {
                "reply": result.reply,
                "is_sensitive": result.is_sensitive,
                "sensitive_type": result.sensitive_type,
                "suggestion": result.suggestion,
                "confidence": result.confidence,
            }
            results.append(c)
        return results

    def batch_generate_stdin(self, video_topic: str = "") -> None:
        """从 stdin 读 JSON 数组（每条含 text/user），输出带回复的 JSON"""
        raw = sys.stdin.buffer.read()
        for enc in ("utf-8-sig", "utf-16", "gbk"):
            try:
                data = json.loads(raw.decode(enc))
                break
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        else:
            raise ValueError("无法解析 stdin 的 JSON 数据")
        results = self.batch_generate(data, video_topic)
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    # 测试 / 批量模式
    if len(sys.argv) > 1 and sys.argv[1] == "--batch":
        topic = sys.argv[2] if len(sys.argv) > 2 else ""
        ReplyGenerator().batch_generate_stdin(topic)
        sys.exit(0)

    gen = ReplyGenerator()

    test_cases = [
        "你们做等保测评吗？",
        "等保三级多少钱？",
        "和奇安信比你们有什么优势？",
        "勒索病毒太可怕了，有什么防护办法吗",
        "学到了，收藏👍",
        "MClaw智能体怎么收费的？",
        "垃圾产品，都是骗人的",
        "怎么合作？加个微信聊聊",
        "你们做数据安全治理吗？",
        "哈哈哈哈哈",
    ]

    print("=== 回复生成测试 ===")
    for text in test_cases:
        result = gen.generate_reply(text)
        print(f"\n评论：{text}")
        print(f"  敏感: {result.is_sensitive} ({result.sensitive_type})")
        print(f"  回复: {result.reply}")
        if result.suggestion:
            print(f"  建议: {result.suggestion}")
