#!/usr/bin/env python3
"""课程质量扫描器 v6 —— 通用版，配置驱动，适配任何课程"""
import sys, os, re, json, argparse
from pathlib import Path

DEFAULT_CONCEPT_KW = r''  # 由 init 自动推断
DEFAULT_SKIP = [r'阶段复习', r'总结', r'靶场实战', r'考核', r'CTF', r'综合项目',
                r'工具演示', r'环境搭建', r'课程导入']

def load_config(course_dir):
    """加载课程配置，没有则用默认值"""
    config_path = Path(course_dir) / "course-config.json"
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)
    return {}

def _natural_key(path):
    """从文件名提取数字做自然排序：课时2 < 课时10"""
    m = re.search(r'(\d+)', Path(path).name)
    return int(m.group(1)) if m else 0

def find_lessons(course_dir, config):
    """找到所有课时文件"""
    doc_dir = Path(course_dir) / "课时文档"
    if not doc_dir.exists():
        doc_dir = Path(course_dir)
    pattern = config.get("doc_pattern", "课时*.md")
    return sorted(doc_dir.glob(pattern), key=_natural_key)

def should_skip(filename, config):
    patterns = config.get("skip_patterns", DEFAULT_SKIP)
    return any(re.search(p, filename) for p in patterns)

def scan_course(course_dir):
    config = load_config(course_dir)
    concept_kw = config.get("concept_keywords", config.get("attack_keywords", DEFAULT_CONCEPT_KW))
    concept_map = config.get("concept_map", {})
    issues = []
    introduced = set()
    lessons = find_lessons(course_dir, config)

    for f in lessons:
        if should_skip(f.name, config):
            continue

        with open(f) as fh:
            content = fh.read()

        # === 规则1：新概念前置 ===
        skip = 0
        m = re.search(r'承接.*?Day|回顾.*?课时|前面.*?学了|上节课', content[:500])
        if m:
            skip = m.end() + 100

        concept_pos = 99999
        if concept_kw:
            for term in concept_kw.split('|'):
                if not term.strip():
                    continue
                p = content.find(term, skip)
                if 0 < p < concept_pos:
                    concept_pos = p

        if concept_pos < 99999:
            before = content[:concept_pos]
            has_prereq = bool(re.search(
                r'前置基础|前置知识|是什么|怎么工作|基本概念|本章定位|快速回顾|基础：',
                before))

            new = []
            for kw, label in (concept_map.items() if concept_map else {}):
                if kw in content[:800] and label not in introduced:
                    new.append(label)

            if new and not has_prereq:
                issues.append({
                    "rule": "前置基础缺失",
                    "file": str(f.relative_to(course_dir)),
                    "msg": f"引入「{'、'.join(new[:3])}」后直接展开深入内容",
                    "fix": f"插入「## 前置基础：{new[0]}是什么」"
                })
            for c in new:
                introduced.add(c)

        # === 规则2：代码可运行 ===
        # 检查代码块中调用的函数/方法是否在上下文中定义
        code_blocks = re.findall(r'```(?:python|java|javascript|bash|go|rust)?\n(.*?)```', content, re.DOTALL)
        all_code = '\n'.join(code_blocks)

        # 检测私有辅助函数是否有定义（如 _check_ssti、_is_concat 等）
        all_func_names = set(re.findall(r'\b([a-zA-Z_]\w+)\s*\(', all_code))
        defined_in_text = set(re.findall(r'def\s+([a-zA-Z_]\w+)\s*\(', all_code))
        defined_in_text.update(re.findall(r'class\s+(\w+)', all_code))
        # 只检查以 _ 开头的私有函数——这些大概率是本课应定义但遗漏的
        private_calls = {f for f in all_func_names if f.startswith('_') and not f.startswith('__')}
        missing_private = private_calls - defined_in_text
        if missing_private:
            issues.append({
                "rule": "代码可能不可运行",
                "file": str(f.relative_to(course_dir)),
                "msg": f"调用了未在文中定义的私有函数：{', '.join(sorted(missing_private)[:5])}",
                "fix": "补全函数定义——这些 _ 开头的方法应在文中给出实现"
            })

        # === 规则3：本地图片 ===
        imgs = re.findall(r'!\[.*?\]\((images/|\.\./images/|\./images/)', content)
        if imgs:
            issues.append({
                "rule": "本地图片",
                "file": str(f.relative_to(course_dir)),
                "msg": f"{len(imgs)} 个本地图片路径",
                "fix": "替换为 jsdelivr URL"
            })

        # === 规则4：原理不跳过 ===
        # 检测"修复/方案/正确做法"段落后代码之后是否有足够解释。
        # 按行处理，定位到标记行，然后检查其后的代码块之后有无解释文字。
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if not re.search(r'\*\*(?:修复|方案|结论|原理|做法|正确做法)\*\*[：:]|^## .*(?:修复|方案|正确做法|防御)', line):
                continue
            # 从标记行后面找代码块和解释文字
            j = i + 1
            code_end = -1
            has_code = False
            while j < len(lines):
                if lines[j].startswith('```'):
                    has_code = True
                    if code_end < 0:
                        code_end = j
                        k = j + 1
                        while k < len(lines):
                            if lines[k].startswith('```'):
                                code_end = k
                                break
                            k += 1
                        j = code_end + 1
                        continue
                    else:
                        code_end = -1
                if code_end >= 0 and j > code_end:
                    break
                j += 1

            # 统计代码块之后的解释文字（如果有代码块）
            if has_code and code_end >= 0:
                j = code_end + 1
                explain_lines = []
                # 也检查标记行本身是否已含解释（如 **结论**：后面直接跟了说明文字）
                inline = re.sub(r'\*\*.*?\*\*[：:]?\s*', '', line).strip()
                if len(inline) > 40:
                    explain_lines.append(inline)
                while j < len(lines) and not lines[j].startswith('## ') and not lines[j].startswith('---'):
                    txt = lines[j].strip()
                    is_short_bold = (len(txt) < 60 and txt.startswith('**') and txt.endswith('**'))
                    if txt and not txt.startswith('![') and not txt.startswith('```') and not is_short_bold:
                        explain_lines.append(txt)
                    j += 1
                explanation = ' '.join(explain_lines)
                if len(explanation) < 80:
                    issues.append({
                        "rule": "原理过简",
                        "file": str(f.relative_to(course_dir)),
                        "msg": f"「{line.strip()[:60]}」只有代码/结论，缺少原理解释({len(explanation)}字)",
                        "fix": "补充2-3句：为什么这样做能解决问题"
                    })

        # === 规则5：衔接断裂 ===
        sections = re.findall(r'## [一二三四五六七八九十]、(.+)', content)
        for i in range(len(sections) - 1):
            curr, nxt = sections[i], sections[i+1]
            curr_pos = content.find(f'## {i+1}、{curr}')
            next_pos = content.find(f'## {i+2}、{nxt}')
            if curr_pos > 0 and next_pos > 0:
                between = content[curr_pos:next_pos]
                if not re.search(r'为什么|对比|替代|局限|不足|优于|区别|演进', between):
                    if not re.search(r'常见|场景|模式|类型|分类|清单', nxt):
                        issues.append({
                            "rule": "衔接断裂",
                            "file": str(f.relative_to(course_dir)),
                            "msg": f"「{curr}」→「{nxt}」无过渡",
                            "fix": "加一句过渡：前者有什么局限→后者怎么解决"
                        })

    return issues


def cmd_init(course_dir):
    """初始化课程配置"""
    config_path = Path(course_dir) / "course-config.json"
    if config_path.exists():
        print(f"配置文件已存在: {config_path}")
        return

    doc_dir = Path(course_dir) / "课时文档"
    if not doc_dir.exists():
        doc_dir = Path(course_dir)
    lessons = sorted(doc_dir.glob("课时*.md"))
    if not lessons:
        print("未找到课时文档，请检查目录")
        return

    # 分析课时内容，自动推断
    all_text = ""
    for f in lessons[:5]:  # 采样前5课
        with open(f) as fh:
            all_text += fh.read()[:5000]

    # 推断概念关键词——从高频技术词汇中提取
    concept_kw = ""
    tech_terms = set(re.findall(r'`([a-zA-Z_]\w+(?:\(\))?)`', all_text))
    tech_terms.update(re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', all_text))
    # 也找中文技术词
    cn_terms = set(re.findall(r'(?:注入|加密|哈希|编码|解码|序列化|反序列化|遍历|递归|闭包|作用域|原型|继承|多态|封装|抽象|接口|泛型|异步|协程|并发|并行|事务|索引|缓存|代理|反射|沙箱|逃逸|签名|令牌|认证|授权|路由|中间件|拦截器|过滤器|监听器|观察者|工厂|单例|适配器|装饰器|策略|模板|状态|命令|责任链|代理)', all_text))
    if cn_terms:
        concept_kw = '|'.join(cn_terms)

    # 推断概念映射——从标题和粗体文本中提取
    concept_map = {}
    bold_terms = re.findall(r'\*\*(.+?)\*\*', all_text)
    heading_terms = re.findall(r'^#+ (.+)$', all_text, re.MULTILINE)
    candidate_terms = bold_terms + heading_terms
    seen = set()
    for term in candidate_terms:
        term = term.strip()
        if len(term) >= 2 and len(term) <= 30 and term not in seen:
            # 用前两个字作键
            key = term[:2]
            if key not in concept_map:
                concept_map[key] = term
                seen.add(term)

    config = {
        "course_name": Path(course_dir).name,
        "concept_keywords": concept_kw,
        "skip_patterns": DEFAULT_SKIP,
        "doc_pattern": "课时*.md",
        "concept_map": concept_map,
    }

    with open(config_path, 'w') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"配置文件已生成: {config_path}")
    print(f"  概念关键词: {concept_kw[:80] if concept_kw else '(未检测到，可手动填写)'}")
    print(f"  概念映射: {len(concept_map)} 条")


def print_report(issues):
    if not issues:
        print("\n✓ 未发现问题\n")
        return
    from itertools import groupby
    for rule, items in groupby(sorted(issues, key=lambda x: x['rule']), key=lambda x: x['rule']):
        items = list(items)
        print(f"\n{'='*50}\n【{rule}】{len(items)} 个\n{'='*50}")
        for i in items:
            print(f"\n  📄 {i['file']}")
            print(f"  → {i['msg']}")
            print(f"  🔧 {i['fix']}")
    print(f"\n总计: {len(issues)} 个问题\n")


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['init', 'scan'])
    p.add_argument('course_dir')
    args = p.parse_args()

    if args.command == 'init':
        cmd_init(args.course_dir)
    else:
        print_report(scan_course(args.course_dir))
