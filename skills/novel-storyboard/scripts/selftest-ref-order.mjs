#!/usr/bin/env node
// 参考图编号（refOrder）自测：<Picture N> 是本段第 N 张角色 / 场景 / 道具设定图，不是分镜序号。
// 复现的原问题：四个素材的提示词里全是 <Picture 1>。每条断言都是「击穿用例」——证明门真的会拦。
// 单独成文件：selftest.mjs 在更早的断言处中断时，这里仍然能单独跑。

import { readFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gateReport, exportPack } from './novel-storyboard.mjs';

const d = dirname(fileURLToPath(import.meta.url)) + '/../';
const J = (p) => JSON.parse(readFileSync(d + p, 'utf8'));
const FIX = J('examples/渡口-storyboard.json');
const CTX = { script: J('references/test-fixtures/upstream/渡口-script.json'), outline: J('references/test-fixtures/upstream/渡口-outline.json'), cast: J('references/test-fixtures/upstream/渡口-cast.json'), art: J('references/test-fixtures/upstream/渡口-art.json') };

const SECTIONS = (defs, detail) => `subject_definitions:\n${defs}\n\nsummary:\ns\n\nretention_analysis:\nr\n\ndetailed_description:\n${detail}\n\noverall_soundscape:\nx\n\nnon_diegetic_music:\nm`;
const ref2v = (doc) => gateReport(doc, CTX).find((g) => g.id === 'ref2v-prompt');
let fails = 0;
const check = (cond, label) => { console.log((cond ? 'PASS ' : 'FAIL ') + label); if (!cond) fails++; };

// 取样例里第一个只有 1 个分镜的段，改造成 H3 Ref2V 段
const make = (h3, refOrder) => {
  const doc = structuredClone(FIX);
  doc.videoModel = 'comfyui_h3'; doc.videoPromptFormat = 'ref2v-v1';
  const seg = doc.episodes[0].segments[0];
  seg.h3Prompt = h3; seg.videoPrompt = h3; seg.videoModel = 'comfyui_h3';
  if (refOrder) seg.refOrder = refOrder; else delete seg.refOrder;
  return { doc, seg };
};
const ORDER = [
  { no: 1, kind: 'character', tag: 'C09', name: '江边村民' }, { no: 2, kind: 'character', tag: 'C02', name: '燕溪山' },
  { no: 3, kind: 'character', tag: 'C03', name: '徐今朝' }, { no: 4, kind: 'scene', tag: 'S01', name: '江边祭坛' },
];
const nCuts = FIX.episodes[0].segments[0].cuts.length;
const bad = (g) => (g && g.detail) ? String(g.detail) : '';
const sid = FIX.episodes[0].segments[0].id;
const only = (g) => bad(g).split('；').filter((x) => x.startsWith(sid));

// 1) 四个素材各自定义、各自编号 → 参考图相关的错误一条都没有
{
  const h3 = SECTIONS('<Picture 1>: villagers\n<Picture 2>: swordsman\n<Picture 3>: sister\n<Picture 4>: altar', '[Shot 1] <Picture 4> altar <Picture 2> <Picture 3> <Picture 1>');
  const { doc } = make(h3, ORDER);
  const errs = only(ref2v(doc)).filter((e) => /参考图|<Picture|subject_definitions/.test(e));
  check(errs.length === 0, `refOrder 编号 1–4、全部定义 → 无参考图报错（实际：${errs.join(' / ') || '无'}）`);
}
// 2) 原 bug：四个素材全写 <Picture 1> → 必须被拦
{
  const h3 = SECTIONS('<Picture 1>: swordsman\n<Picture 1>: sister\n<Picture 1>: villagers\n<Picture 1>: altar', '[Shot 1] <Picture 1> everything');
  const { doc } = make(h3, ORDER);
  const errs = only(ref2v(doc));
  check(errs.some((e) => /没有定义参考图/.test(e) && /2=燕溪山/.test(e) && /4=江边祭坛/.test(e)), `全是 Picture 1 → 报出缺定义的 2/3/4（实际：${errs.join(' / ')}）`);
}
// 3) 超出范围的编号
{
  const h3 = SECTIONS('<Picture 1>: a\n<Picture 2>: b\n<Picture 3>: c\n<Picture 4>: d', '[Shot 1] <Picture 5> x');
  const { doc } = make(h3, ORDER);
  check(only(ref2v(doc)).some((e) => /编号 5 超出本段参考图范围 1–4/.test(e)), '编号 5 超出 1–4 → 拦住');
}
// 4) 残留的素材 ID 占位符（程序没能解析）
{
  const h3 = SECTIONS('<Picture 1>: a\n<Picture 2>: b\n<Picture 3>: c\n<Picture 4>: d', '[Shot 1] <Picture C99> x <Picture 1>');
  const { doc } = make(h3, ORDER);
  check(only(ref2v(doc)).some((e) => /<Picture C99> 对不上本段任何参考图/.test(e)), '残留 <Picture C99> → 拦住');
}
// 5) 旧分镜（没有 refOrder）仍走原规则：编号必须 1..分镜数
{
  const h3 = SECTIONS('<Picture 1>: a', '[Shot 1] <Picture 1> x');
  const { doc } = make(h3, null);
  const errs = only(ref2v(doc));
  check(nCuts === 1 ? true : errs.some((e) => /参考图编号应连续对应 1–/.test(e)), `旧分镜无 refOrder 走旧规则（段有 ${nCuts} 个分镜；${errs[0] ?? '无报错'}）`);
}
// 6) 导出包：有 refOrder 时 prompt.md 不再把 Picture k 说成 f{k}.png
{
  const { doc } = make(SECTIONS('<Picture 1>: a', '[Shot 1] <Picture 1>'), ORDER);
  const pack = exportPack(doc, CTX.script, { dir: '.' });
  const md = pack.files.find((f) => f.path.endsWith(`${sid}/prompt.md`)).content;
  check(md.includes('Picture 2 = 角色「燕溪山」') && md.includes('Picture 4 = 场景「江边祭坛」') && !md.includes('f1.png（**首帧**'), 'prompt.md 头部按设定图列 Picture 对照，不再写 f1.png 首帧');
  const m = pack.manifest.find((x) => x.segment === sid);
  check(Array.isArray(m.references) && m.references.length === 4 && m.references[1].name === '燕溪山', 'manifest.json 带 references 对照表');
}
console.log(fails ? `\n${fails} FAILED` : '\nALL PASS');
process.exit(fails ? 1 : 0);
