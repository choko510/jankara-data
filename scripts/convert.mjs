// 料金表を金額へ変換する。アプリに配るのは変換できた店の金額だけ
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { convertBigEcho } from './web/bigecho.js';
import { convertManeki } from './web/maneki.js';
import { convertMegabig } from './web/megabig.js';
import { convertKanOcr } from './web/ocr.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const FIELDS = ['regular', 'solo', 'week', 'flat', 'minStay', 'alerts'];
// カラオケ館は料金表が画像なので、ocr.yml が読んだ結果から作る。ocr.json には他のチェーンの店も入っている
const TARGETS = {
  bigecho: ['chain_prices.json', (shop) => convertBigEcho(shop.sections, shop.hours)],
  maneki: ['maneki_prices.json', (shop) => convertManeki(shop.tables)],
  megabig: ['megabig_prices.json', (shop) => convertMegabig(shop.sections, shop.hours)],
  kan: ['ocr.json', (shop) => (shop.chain === 'カラオケ館' ? convertKanOcr(shop.reads) : { reason: 'other-chain' })],
};

function isEmpty(value) {
  return value === undefined || value === null || (Array.isArray(value) && !value.length);
}

mkdirSync(join(ROOT, 'build'), { recursive: true });
for (const [name, [file, convert]] of Object.entries(TARGETS)) {
  const source = JSON.parse(readFileSync(join(ROOT, 'source', file), 'utf8'));
  const shops = {};
  let total = 0;
  for (const [id, shop] of Object.entries(source.shops)) {
    const converted = convert(shop);
    if (converted.reason !== 'other-chain') total += 1;
    if (converted.reason) continue;
    shops[id] = { checked_at: shop.checked_at };
    for (const field of FIELDS) if (!isEmpty(converted[field])) shops[id][field] = converted[field];
  }

  console.log(`${name}: ${Object.keys(shops).length} / ${total}`);
  writeFileSync(join(ROOT, 'build', `${name}.json`), JSON.stringify({ updated_at: source.updated_at, shops }));
}
 
// ROUND1 の B-30 画像は通常のカラオケ館 OCR とは別形式。
// 目視確認・画像SHA256照合済みの店舗だけ配信する。金額未確認の初期状態で
// 空の round1.json を配信して「対応店舗あり」と誤認させない。
const round1Path = join(ROOT, 'source', 'round1_b30.json');
if (existsSync(round1Path)) {
  const data = JSON.parse(readFileSync(round1Path, 'utf8'));
  if (data.schema_version !== 1 || !Number.isInteger(data.updated_at) ||
      !data.shops || typeof data.shops !== 'object' || Array.isArray(data.shops)) {
    throw new Error('source/round1_b30.json: invalid format');
  }
  const registry = JSON.parse(readFileSync(join(ROOT, 'chains.json'), 'utf8'));
  const registered = new Map((Array.isArray(registry.shops) ? registry.shops : Object.values(registry.shops))
    .map((shop) => [shop.id, shop]));
  const shops = {};
  for (const [id, shop] of Object.entries(data.shops)) {
    if (!/^r1-[A-Za-z0-9_-]+$/.test(id) || registered.get(id)?.chain !== 'ROUND1') {
      throw new Error(`source/round1_b30.json: unknown ROUND1 shop ${id}`);
    }
    if (!Number.isInteger(shop.checked_at) || !Array.isArray(shop.regular) ||
        shop.regular.length !== 1 || shop.regular[0]?.band !== '30分' ||
        !Array.isArray(shop.regular[0]?.plans) || !shop.regular[0].plans.length) {
      throw new Error(`source/round1_b30.json: invalid regular table for ${id}`);
    }
    for (const plan of shop.regular[0].plans) {
      if (plan.name !== '30分' || typeof plan.hours !== 'string' ||
          !Array.isArray(plan.days) || plan.days.length !== 5) {
        throw new Error(`source/round1_b30.json: invalid plan for ${id}`);
      }
      for (const row of plan.days) {
        if (!['日祝', '土', '金祝前', '月火水木', '平日'].includes(row.day) ||
            !row.prices || typeof row.prices !== 'object' || Array.isArray(row.prices) ||
            Object.entries(row.prices).some(([customer, price]) =>
              !['学生会員', '学生', 'シニア', '一般'].includes(customer) ||
              !Number.isInteger(price) || price < 30 || price > 2000)) {
          throw new Error(`source/round1_b30.json: invalid day prices for ${id}`);
        }
      }
    }
    shops[id] = { checked_at: shop.checked_at, regular: shop.regular };
  }
  if (Object.keys(shops).length) {
    writeFileSync(join(ROOT, 'build', 'round1.json'), JSON.stringify({
      updated_at: data.updated_at, shops,
    }));
    console.log(`round1: ${Object.keys(shops).length} verified shops`);
  }
}

// JOYJOYは公式ページに掲載された「～」付きの最低価格を保つ。通常料金へ
// 変換すると実額と誤認されるため、単位・時間帯・最低価格をそのまま配る。
const joyjoy = JSON.parse(readFileSync(join(ROOT, 'source', 'joyjoy_prices.json'), 'utf8'));
writeFileSync(join(ROOT, 'build', 'joyjoy.json'), JSON.stringify({
  updated_at: joyjoy.updated_at,
  shops: joyjoy.shops,
}));
console.log(`joyjoy: ${Object.keys(joyjoy.shops).length} 店 (公式最低料金)`);
