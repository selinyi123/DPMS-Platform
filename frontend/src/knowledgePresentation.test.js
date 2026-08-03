import assert from 'node:assert/strict';
import test from 'node:test';

import { knowledgeGapPresentation } from './knowledgePresentation.js';

const dictionary = {
  'knowledge.gaps.result_labels_low.label': '结果标签',
  'knowledge.gaps.result_labels_low.title': '结果标签数量不足',
  'knowledge.gaps.result_labels_low.detail': '记录每次已知开奖结果。',
  'knowledge.gaps.unknown.label': '未分类缺项',
  'knowledge.gaps.unknown.title': '存在未分类的学习缺项',
  'knowledge.gaps.unknown.detail': '刷新并检查事件记录。',
};
const t = key => dictionary[key] || key;

test('localizes learning-gap code, title, and detail by stable code', () => {
  assert.deepEqual(knowledgeGapPresentation({
    code: 'result_labels_low',
    title: 'Lottery result labels are too sparse',
    detail: 'English backend detail',
  }, t), {
    label: '结果标签',
    title: '结果标签数量不足',
    detail: '记录每次已知开奖结果。',
  });
});

test('unknown backend copy fails closed to localized generic guidance', () => {
  const result = knowledgeGapPresentation({
    code: 'new_backend_gap',
    title: 'Untranslated backend title',
    detail: 'Untranslated backend detail',
  }, t);
  assert.deepEqual(result, {
    label: '未分类缺项',
    title: '存在未分类的学习缺项',
    detail: '刷新并检查事件记录。',
  });
  assert.doesNotMatch(JSON.stringify(result), /Untranslated|new_backend_gap/);
});

