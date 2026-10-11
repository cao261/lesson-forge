"""回炉修订的防丢内容自检。

实测过的真实故障：第二轮修订把教学目标、课堂流程、课后作业、例题答案全洗成空，
规则体检问题从 2 个反弹到 7 个——比不修订还糟。这里验证修复。
"""
from app.models import Example, KnowledgePoint, Lesson, LessonMeta
from app.stages.compose import _to_lesson
from app.stages.review import _content_score

good = Lesson(
    meta=LessonMeta(title="测试"),
    objectives=["目标1", "目标2", "目标3"],
    key_points=["重点1", "重点2"],
    difficulties=["难点1"],
    materials=["白板", "草稿纸"],
    # 作业必须能在视频例题里找到出处，否则会被 _ground_homework 当编造拦掉。
    # ex1 的题干是「若 $x>0$」，数字指纹是 {0}，所以这里的作业只能用 0 和纯文字。
    homework=["把 x>0 这个情形再练一遍", "整理本节笔记"],
    common_mistakes=["易错1"],
    knowledge=[KnowledgePoint(
        id="kp1", title="知识点", summary="讲了什么", definition="定义",
        formula_latex=r"A \subseteq B", key_points=["要点1", "要点2"], pitfalls=["易错1"],
        teaching_notes="先问再讲，让学生先说",
        examples=[Example(id="ex1", title="例题", statement_latex=r"若 $x>0$",
                          answer=r"$m>3$", solution=["第一步", "第二步", "第三步"])],
    )],
)

before = _content_score(good)
print("修订前内容分:", before)

# 模型只重写了 answer 和 title，其余字段全部漏掉（真实故障的复现）
partial = {"knowledge": [{"id": "kp1", "examples": [
    {"id": "ex1", "title": "例题：求参数范围", "answer": "$m>3$，范围是开区间"}]}]}
# refine() 传的就是上一版的 knowledge 作为 kps，示例 id 才能对上
revised = _to_lesson(partial, good.meta, good.knowledge, 90, fallback=good)
after = _content_score(revised)

print("修订后内容分:", after)
checks = [
    ("教学目标没被洗掉", bool(revised.objectives)),
    ("作业没被洗掉", bool(revised.homework)),
    ("重点难点没被洗掉", bool(revised.key_points) and bool(revised.difficulties)),
    ("教学准备没被洗掉", bool(revised.materials)),
    ("讲法提示没被洗掉", bool(revised.knowledge[0].teaching_notes)),
    ("解析步骤没被洗掉", len(revised.knowledge[0].examples[0].solution) == 3),
    ("题干没被洗掉", bool(revised.knowledge[0].examples[0].statement_latex)),
    ("答案保留了模型的新版本", revised.knowledge[0].examples[0].answer == "$m>3$，范围是开区间"),
    ("标题采用了模型的订正", revised.knowledge[0].examples[0].title == "例题：求参数范围"),
    ("内容分没有下降", after >= before),
]
print()
for name, ok in checks:
    print(f"  {'✅' if ok else '❌'} {name}")
print()
print(f"总计：{'全部通过 ✅' if all(c[1] for c in checks) else '有失败项 ❌'}")