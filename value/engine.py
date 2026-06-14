"""价值挖掘五步智能体引擎。

与 Agent 的五步交互逻辑（每步独立角色 persona + 结构化 JSON 输出，前一步结论作为后一步约束）：
1. 逆向拆解瓶颈：产业研究专家，逆向工程拆解 BOM，识别扩产周期长/技术门槛高/不可替代的物理级瓶颈
2. 锁定非对称标的：基于瓶颈，筛选 30-150 亿、机构覆盖低的中小盘隐形冠军（低成本占比 + 高失效风险）
3. 穿透财务拐点：注入真实毛利率/CapEx 数据，验证毛利率拐点与资本开支秘密爬坡
4. AI 红队测试：空头分析师，从技术替代/大客户自研/供应链断裂三维度证伪，穷尽"死路"
5. 指定熔断机制：拟定未来 6 个月可证伪里程碑（订单/良率/专利…），未达成即判定逻辑失效强制清仓
"""
from __future__ import annotations

import json
import re
from typing import Optional

from agents.llm import get_llm
from value import data as vdata


def _parse_json(text: str) -> dict:
    """从 LLM 文本中稳健解析 JSON（去除 ```json 围栏、截取首个 {...}）。"""
    if not text:
        return {}
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # 截取最外层大括号
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        frag = t[start:end + 1]
        try:
            return json.loads(frag)
        except Exception:
            pass
    return {"_raw": text}


def _is_symbol(s: str) -> bool:
    s = (s or "").strip()
    return s.isdigit() and len(s) == 6


class ValueEngine:
    """五步价值挖掘编排器。"""

    def __init__(self):
        self.llm = get_llm()

    def _ask(self, system: str, user: str, max_tokens: int = 4000,
             temperature: float = 0.4) -> dict:
        raw = self.llm.chat(system, user, temperature=temperature,
                            max_tokens=max_tokens, json_mode=True)
        out = _parse_json(raw)
        out.setdefault("_narrative", raw if "_raw" in out else "")
        return out

    # ---------------- 第1步：逆向拆解瓶颈 ----------------
    def step1_bottlenecks(self, target: str) -> dict:
        system = (
            "你是顶尖产业研究专家，擅长对制造业/科技产业链做逆向工程。"
            "你能从最终产品出发，逐层拆解物料清单(BOM)与工艺路线，定位真正的‘物理级瓶颈’——"
            "即扩产周期长、技术门槛高、且短期不可替代的卡脖子环节。只输出严格 JSON，不要多余文字。"
        )
        user = (
            f"目标行业或标的：{target}\n\n"
            "请逆向工程拆解其产业链 BOM，并识别物理级瓶颈环节。严格输出如下 JSON：\n"
            "{\n"
            '  "industry": "归纳的核心产业/赛道",\n'
            '  "end_product": "最终产品",\n'
            '  "bom_layers": [{"layer":"层级名(如 整机/模组/材料)","items":["关键物料/环节"]}],\n'
            '  "bottlenecks": [{\n'
            '     "link":"瓶颈环节名称",\n'
            '     "barrier_type":["扩产周期长","技术门槛高","不可替代"]中适用的标签,\n'
            '     "expansion_cycle":"扩产/认证周期(如 18-24个月)",\n'
            '     "tech_barrier":"技术壁垒说明",\n'
            '     "irreplaceable":"为何短期不可替代",\n'
            '     "severity":"high或medium"\n'
            "  }],\n"
            '  "summary":"一句话总结最关键的瓶颈"\n'
            "}\n"
            "bottlenecks 至少给 2-4 个，按 severity 降序。"
        )
        return self._ask(system, user)

    # ---------------- 第2步：锁定非对称标的 ----------------
    def step2_targets(self, target: str, s1: dict) -> dict:
        bottlenecks = json.dumps(s1.get("bottlenecks", []), ensure_ascii=False)
        system = (
            "你是专挖‘隐形冠军’的中小盘研究员。你寻找的是：卡住瓶颈环节、但市值仅 30-150 亿、"
            "机构覆盖率低、在客户成本中占比低却失效风险高（一旦失效整机报废）的 A股上市公司。"
            "这类标的具备非对称收益：下行有限、上行巨大。只输出严格 JSON。"
        )
        user = (
            f"目标：{target}\n物理级瓶颈环节：{bottlenecks}\n\n"
            "基于瓶颈环节，筛选 A股中小盘隐形冠军。严格输出 JSON：\n"
            "{\n"
            '  "targets": [{\n'
            '     "name":"公司简称",\n'
            '     "symbol":"6位A股代码(必须真实, 无法确定则留空字符串)",\n'
            '     "bottleneck":"对应卡位的瓶颈环节",\n'
            '     "market_cap_hint":"市值量级估计(如 约80亿)",\n'
            '     "coverage":"机构覆盖度(低/中/高)",\n'
            '     "cost_ratio":"在下游成本中占比(低/中/高)",\n'
            '     "failure_risk":"失效风险(高/中/低)",\n'
            '     "moat":"护城河/为何是隐形冠军",\n'
            '     "asymmetry":"非对称收益逻辑"\n'
            "  }],\n"
            '  "note":"筛选说明与风险提示"\n'
            "}\n"
            "优先给出市值 30-150 亿、覆盖度低、成本占比低且失效风险高的标的，给 2-4 个，最相关的排第一。"
            "代码务必准确，拿不准就留空，不要编造。"
        )
        return self._ask(system, user)

    # ---------------- 第3步：穿透财务拐点 ----------------
    def step3_financial(self, primary: dict, fin_text: str) -> dict:
        system = (
            "你是财务侦探，擅长从财报里读出供需拐点的早期信号。你重点看两件事："
            "①近两季度毛利率是否因供需失衡出现‘爆发式拐点’（环比加速上行）；"
            "②CapEx/在建工程是否在‘秘密爬坡’以承接未来需求（资本开支领先于收入）。只输出严格 JSON。"
        )
        name = primary.get("name") or primary.get("symbol") or "该标的"
        user = (
            f"标的：{name}（{primary.get('symbol','')}）\n"
            f"真实财务数据：\n{fin_text}\n\n"
            "请审查并严格输出 JSON：\n"
            "{\n"
            '  "gross_margin_inflection": {"verdict":"出现拐点/初现/未出现/数据不足","evidence":"结合具体数值说明"},\n'
            '  "capex_ramp": {"verdict":"明显爬坡/温和/未见/数据不足","evidence":"结合具体数值说明"},\n'
            '  "supply_demand_read":"对供需格局的判断",\n'
            '  "conclusion":"财务面是否支持瓶颈逻辑兑现",\n'
            '  "score": 0到100的整数(财务拐点确认度)\n'
            "}\n"
            "若数据缺失，verdict 用‘数据不足’并基于公开信息谨慎定性，不要编造数字。"
        )
        return self._ask(system, user)

    # ---------------- 第4步：AI 红队证伪 ----------------
    def step4_redteam(self, primary: dict, s1: dict, s3: dict) -> dict:
        system = (
            "你是最严苛的空头分析师（红队）。你的唯一任务是证伪多头逻辑，穷尽该标的的所有‘死路’。"
            "必须从三个维度无情攻击：①技术路径替代（新技术使该环节被绕过）；"
            "②大客户自研/垂直整合（客户自己干掉它）；③供应链断裂（上游断供/被国产替代反噬）。只输出严格 JSON。"
        )
        name = primary.get("name") or primary.get("symbol") or "该标的"
        ctx = (
            f"标的：{name}（{primary.get('symbol','')}）\n"
            f"瓶颈逻辑：{s1.get('summary','')}\n"
            f"财务结论：{(s3.get('conclusion') or '')[:200]}"
        )
        user = (
            ctx + "\n\n撰写证伪报告，严格输出 JSON：\n"
            "{\n"
            '  "short_thesis": [{\n'
            '     "dimension":"技术路径替代/大客户自研/供应链断裂",\n'
            '     "argument":"具体证伪论证",\n'
            '     "probability":"高/中/低",\n'
            '     "kill_condition":"出现什么信号即证明多头逻辑死亡"\n'
            "  }],\n"
            '  "fatal_risk":"其中最致命的一条",\n'
            '  "verdict":"逻辑稳健/存在重大风险/逻辑证伪",\n'
            '  "summary":"红队总结"\n'
            "}\n"
            "三个维度每个至少一条，论证要具体、可证伪，不要泛泛而谈。"
        )
        return self._ask(system, user)

    # ---------------- 第5步：指定熔断机制 ----------------
    def step5_circuit(self, primary: dict, s1: dict, s4: dict) -> dict:
        system = (
            "你是纪律严明的风控官。你要把投资逻辑转化为‘可证伪的关键里程碑’，"
            "为未来6个月设定订单/良率/专利/产能/客户等核心可观测事件，并明确：若到期未达成，"
            "即判定逻辑失效、强制清仓。只输出严格 JSON。"
        )
        name = primary.get("name") or primary.get("symbol") or "该标的"
        user = (
            f"标的：{name}（{primary.get('symbol','')}）\n"
            f"核心瓶颈逻辑：{s1.get('summary','')}\n"
            f"主要风险：{(s4.get('fatal_risk') or '')[:150]}\n\n"
            "拟定未来6个月可证伪里程碑，严格输出 JSON：\n"
            "{\n"
            '  "milestones": [{\n'
            '     "window":"时间窗(如 M+1、未来1-2个月)",\n'
            '     "event_type":"订单/良率/专利/产能/客户/价格",\n'
            '     "milestone":"具体可证伪事件",\n'
            '     "success_criteria":"达成标准(尽量量化)",\n'
            '     "fail_action":"未达成的处置(减仓/清仓)",\n'
            '     "priority":"核心/次要"\n'
            "  }],\n"
            '  "circuit_breaker":"总熔断规则：满足什么条件即强制清仓",\n'
            '  "review_cadence":"复核节奏(如 每月底)"\n'
            "}\n"
            "里程碑给 4-6 个，覆盖订单、良率、专利等不同事件类型，至少 2 个标记为‘核心’。"
        )
        return self._ask(system, user)

    # ---------------- 主流程 ----------------
    def run(self, target: str, focus_symbol: str = "", persist: bool = True) -> dict:
        target = (target or "").strip()
        if not target:
            return {"error": "请输入行业或标的"}
        if not self.llm.available:
            return {
                "error": "价值挖掘为 LLM 驱动的智能体工作流，需配置 DeepSeek（DEEPSEEK_API_KEY 且 ENABLE_LLM=true）后使用。",
                "llm_required": True,
            }

        # 第1步
        s1 = self.step1_bottlenecks(target)
        # 第2步
        s2 = self.step2_targets(target, s1)
        s2["targets"] = vdata.validate_targets(s2.get("targets", []))

        # 选定主标的：用户指定 > 命中市值区间的首个 > 含代码的首个 > 无
        primary_symbol = focus_symbol.strip() if _is_symbol(focus_symbol) else ""
        if not primary_symbol and _is_symbol(target):
            primary_symbol = target.strip()
        if not primary_symbol:
            for t in s2["targets"]:
                if t.get("in_cap_range") and _is_symbol(str(t.get("symbol", ""))):
                    primary_symbol = t["symbol"]
                    break
        if not primary_symbol:
            for t in s2["targets"]:
                if _is_symbol(str(t.get("symbol", ""))):
                    primary_symbol = t["symbol"]
                    break

        # 主标的财务快照（注入第3步）
        if primary_symbol:
            snap = vdata.target_snapshot(primary_symbol)
        else:
            # 无确定 A股代码：用 step2 首个标的的定性信息
            t0 = (s2["targets"][0] if s2.get("targets") else {}) or {}
            snap = {"symbol": t0.get("symbol", ""), "name": t0.get("name", target),
                    "market_cap_yi": None, "gross_margin_trend": [], "net_margin_trend": [],
                    "capex": {}}

        fin_text = self._fin_text(snap)

        # 第3步
        s3 = self.step3_financial(snap, fin_text)
        # 第4步
        s4 = self.step4_redteam(snap, s1, s3)
        # 第5步
        s5 = self.step5_circuit(snap, s1, s4)

        result = {
            "target": target,
            "focus_symbol": focus_symbol,
            "primary": snap,
            "bottlenecks": s1,
            "candidates": s2,
            "financial": s3,
            "redteam": s4,
            "circuit": s5,
            "llm_enabled": True,
        }
        if persist:
            try:
                from value.store import save_analysis
                rec = save_analysis(result)
                result["id"] = rec["id"]
                result["created_at"] = rec["created_at"]
            except Exception:
                pass
        return result

    @staticmethod
    def _fin_text(snap: dict) -> str:
        lines = [
            f"公司：{snap.get('name','')}（{snap.get('symbol','')}）",
            f"总市值：{snap.get('market_cap_yi','未知')} 亿"
            + (f"｜PE:{snap.get('pe')} PB:{snap.get('pb')} ROE:{snap.get('roe')}"
               if snap.get("pe") is not None else ""),
            f"毛利率趋势：{vdata.fmt_trend(snap.get('gross_margin_trend', []), 'gross_margin')}",
            f"净利率趋势：{vdata.fmt_trend(snap.get('net_margin_trend', []), 'value')}",
        ]
        cap = snap.get("capex") or {}
        if cap.get("series"):
            label = "CapEx(资本开支)" if cap.get("kind") == "capex" else "在建工程(扩产代理)"
            lines.append(f"{label}趋势：{vdata.fmt_trend(cap['series'], 'capex', '亿')}")
        else:
            lines.append("CapEx/在建工程：数据暂缺")
        if snap.get("revenue_growth") is not None:
            lines.append(f"营收同比:{snap.get('revenue_growth')}% 净利同比:{snap.get('profit_growth')}%")
        return "\n".join(lines)


def run_value(target: str, focus_symbol: str = "") -> dict:
    return ValueEngine().run(target, focus_symbol)
