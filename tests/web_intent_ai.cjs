// AI 意向判定「模型来源」预设：交付现场只贴 Key，不该手敲 URL。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/app.js', 'utf8');

function fn(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
  assert.ok(match, name);
  const next = source.indexOf('\n', match.index) + 1;
  if (source.slice(match.index, next).trimEnd().endsWith('}')) return source.slice(match.index, next);
  const end = /^}\r?$/m.exec(source.slice(next));
  return source.slice(match.index, next + end.index + 1);
}
const presets = /const INTENT_AI_PRESETS = \{[\s\S]*?\n\};/.exec(source)[0];

function fixture(values = {}) {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, { id, value: values[id] || '', placeholder: '' });
    return nodes.get(id);
  };
  const context = vm.createContext({ $, Object });
  vm.runInContext(presets, context);
  vm.runInContext(fn('applyIntentAiPreset'), context);
  vm.runInContext(fn('syncIntentAiPreset'), context);
  return { context, $, run: code => vm.runInContext(code, context) };
}

// 1) 选千问：地址、模型、批量、超时一次填好，Key 提醒贴
{
  const f = fixture();
  const msg = f.run(`applyIntentAiPreset('qwen')`);
  assert.equal(f.$('intent-ai-base').value, 'https://dashscope.aliyuncs.com/compatible-mode/v1');
  assert.equal(f.$('intent-ai-model').value, 'qwen3.7-flash');
  assert.equal(f.$('intent-ai-batch').value, '16');
  assert.equal(f.$('intent-ai-timeout').value, '30');
  assert.match(f.$('intent-ai-key').placeholder, /必填/);
  assert.match(msg, /试判几条/);
}
// 2) 本机 Ollama：不该逼客户填 Key
{
  const f = fixture();
  f.run(`applyIntentAiPreset('ollama')`);
  assert.equal(f.$('intent-ai-base').value, 'http://localhost:11434/v1');
  assert.equal(f.$('intent-ai-timeout').value, '120');   // 首次加载权重慢
  assert.equal(f.$('intent-ai-key').placeholder, '');
}
// 3) 空选项与未知选项不动已有填写
{
  const f = fixture({ 'intent-ai-base': 'https://custom.example/v1', 'intent-ai-model': 'mine' });
  assert.equal(f.run(`applyIntentAiPreset('')`), '');
  assert.equal(f.run(`applyIntentAiPreset('nope')`), '');
  assert.equal(f.$('intent-ai-base').value, 'https://custom.example/v1');
  assert.equal(f.$('intent-ai-model').value, 'mine');
}
// 4) 回填来源：尾斜杠、未登记的自建地址
{
  const f = fixture({ 'intent-ai-base': 'https://dashscope.aliyuncs.com/compatible-mode/v1/',
                      'intent-ai-preset': 'deepseek' });
  assert.equal(f.run(`syncIntentAiPreset()`), 'qwen');
  assert.equal(f.$('intent-ai-preset').value, 'qwen');
  f.$('intent-ai-base').value = 'https://ws-abc.cn-beijing.maas.aliyuncs.com/compatible-mode/v1';
  assert.equal(f.run(`syncIntentAiPreset()`), '');
  assert.equal(f.$('intent-ai-preset').value, '');
}
// 5) 下拉里的选项值必须和预设表对得上，否则选了没反应
{
  const f = fixture();
  const keys = JSON.parse(JSON.stringify(f.run(`Object.keys(INTENT_AI_PRESETS)`)));
  const html = fs.readFileSync('app/web/index.html', 'utf8');
  const block = html.slice(html.indexOf('id="intent-ai-preset"'));
  const opts = [...block.matchAll(/<option value="([^"]*)"/g)].slice(1, 1 + keys.length).map(m => m[1]);
  assert.deepEqual(opts, keys);
  for (const key of keys) {
    const preset = JSON.parse(JSON.stringify(f.run(`INTENT_AI_PRESETS['${key}']`)));
    assert.ok(/^https?:\/\/.+/.test(preset.base), key + ' 地址不合法');
    assert.ok(preset.model && preset.batch && preset.timeout, key + ' 预设字段不全');
  }
}
console.log('intent-ai presets passed');
