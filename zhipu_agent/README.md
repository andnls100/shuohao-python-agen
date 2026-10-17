# zhipu_agent：不需要 Codex 的技能包驱动器

使用 Python 加任意 OpenAI 兼容的大模型接口，按顺序驱动本仓库的七个技能包。
技能包本身（`skills/`）一个字没改：模型读它的 `references/*-pass.md` 和 `schema.md` 当作指令，
技能包自带的 Node 脚本（`validate`、`merge`、`assemble`、`render`……）当作质量门。

## 工作方式

流程固定，由 Python 编排，不让模型自己决定下一步：

1. 大纲（novel-outline）：骨架先出，等你确认，再分批写每集。
2. 角色（novel-characters）：扫描、归并、逐个角色出卡。
3. 美术（novel-art）：场景和叙事道具。
4. 剧本（novel-script）：三集一批。
5. 站位（novel-staging）：先出地点平面，再按地点分组写场景。
6. 分镜（novel-storyboard）：三集一批，最后导出清单。
7. 角色参考图（character-refs）：可选，默认不跑。

每个阶段共用同一个循环：并发生成每个条目 → 写入目标文件 → 运行技能包的 `validate` →
把报错按集号、场景名、角色名归给具体条目 → 只让出错的条目带着报错回炉，最多 `AGENT_FIX_ROUNDS` 轮。
每个条目的结果缓存在 `output/work/`，中断后重跑会接着做；已完成且通过校验的阶段会被跳过。

## 安装

```
pip install -r requirements.txt     # 只需要 openai 包
node --version                      # 需要 Node 18 及以上
cp .env.example .env                # 填 ZHIPU_API_KEY 和 ZHIPU_MODEL
python run_agent.py doctor --ping
```

## 使用

```
python run_agent.py run --book input/novel.txt --episodes 6 --minutes 2 --genre 都市
python run_agent.py run --stages script,staging,storyboard --eps 1-3
python run_agent.py run --stages refs --refs-names 沈知微
python run_agent.py run --stages continuity
python run_agent.py status
```

如果 `python -m zhipu_agent.main` 报找不到模块（Python 被设成了隔离模式），就使用根目录的 `run_agent.py`，参数完全一样。

原文是 UTF-8 或 GBK 都可以，会先转成 UTF-8 放到 `output/source/`。
骨架拍板点默认会暂停等你回复；加 `--yes` 自动确认。

## 已验证与未验证

已验证（离线，`python tests/test_offline_pipeline.py`）：使用模拟模型加真实的 Node 校验器，
六个阶段从《渡口》样例一路跑到分镜并通过各自的质量门；校验不过时只有出错的集和角色回炉；
再跑一遍会全部跳过；长文本走分卷摘要；带图条目会发给视觉模型。

没有验证：

- 没有使用真实模型跑过。离线测试只证明接线正确，不能说明真实模型写得好不好、几轮回炉能过。
- 视觉模型的图片消息格式按 OpenAI 的 `image_url` 写法实现，没有在智谱或你的聚合平台上确认。
- `--refs-gen` 的真实出图没有测过；character-refs 的作者自己也只使用假服务验证过。

长篇小说的成本较高：每个条目至少一次调用，回炉另算。先使用 `--eps` 和短文本试跑。


## 一致性视频资产清单

在已有角色设定、美术设定和分镜产物后运行：

```
python run_agent.py run --stages continuity
```

会生成 `output/continuity/continuity-manifest.json`，登记角色、场景、道具资产，以及每个分镜计划使用的参考素材和镜头参考图目标路径。该阶段只整理清单，不调用生图或视频 API。角色关联会结合镜头文字和已有 staging 站位做匹配；请重点检查清单中的 `scene_id`、`character_ids`，自动匹配可能需要人工修正。
