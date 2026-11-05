// メガビッグ公式料金表を料金ナビの regular / solo 形式に変換する。
// 表の曜日・料金区分・時間帯のどれかを読めない場合、その店の料金は配信しない。
import { dayLabels, parseDays, parseTimeRange } from './bigecho.js';

const CUSTOMERS = ['学生会員', '学生', '会員', 'シニア', '一般'];
const DAY_ORDER = ['月', '火', '水', '木', '金', '土', '日', '祝前', '祝'];
const norm = (text) => (text ?? '').normalize('NFKC').replace(/\s+/g, '');

function weekDays(label) {
  const parsed = parseDays(label);
  if (!parsed) return null;
  return parsed;
}

// 公式の「月～木 11:00～5:00」「全日 10時～翌5時」を engine の week 形式にする。
export function parseMegabigHours(text) {
  const normalized = norm(text)
    .replace(/(\d{1,2})時(?:(\d{1,2})分)?/g, (_, hour, minute) =>
      `${hour}:${String(minute ?? '00').padStart(2, '0')}`)
    .replace(/翌/g, '');
  const ranges = [...normalized.matchAll(/(\d{1,2}:\d{2})[~〜～-](\d{1,2}:\d{2})/g)];
  if (!ranges.length) return null;
  const week = {};
  let cursor = 0;
  for (const match of ranges) {
    const prefix = normalized.slice(cursor, match.index);
    const labelMatch = /(?:全日|[月火水木金土日祝前曜祭・,、~〜～/-]+)$/.exec(prefix);
    const label = labelMatch?.[0] || (ranges.length === 1 ? '全日' : '');
    const days = weekDays(label);
    if (!days) return null;
    const open = clock(match[1]);
    let close = clock(match[2]);
    while (close <= open) close += 1440;
    for (const day of days) {
      const key = day === '祝' ? '日祝' : day;
      if (week[key] && (week[key][0] !== open || week[key][1] !== close)) return null;
      week[key] = [open, close];
      if (day === '日') week['日祝'] ??= [open, close];
    }
    cursor = match.index + match[0].length;
  }
  return Object.keys(week).length ? week : null;
}

function clock(text) {
  const [hour, minute] = text.split(':').map(Number);
  return hour * 60 + minute;
}

function customerName(value) {
  const name = norm(value);
  if (name === '学生会員') return '学生会員';
  if (name === '学生' || name === '学生一般') return '学生';
  if (name === '会員') return '会員';
  if (name === '一般') return '一般';
  if (/^シニア(?:会員)?/.test(name)) return 'シニア';
  return null;
}

function readDays(section) {
  const columns = section.header.slice(1).map((label) => parseDays(label));
  if (!columns.length || columns.some((days) => !days)) return null;
  const mapped = dayLabels(columns);
  if (!mapped) return null;
  const values = new Map();
  for (const row of section.rows) {
    const customer = customerName(row[0]);
    if (!customer || values.has(customer) || row.length !== columns.length + 1) return null;
    const prices = row.slice(1).map((value) => {
      const match = /^(\d[\d,]*)$/.exec(norm(value));
      return match ? Number(match[1].replace(/,/g, '')) : null;
    });
    if (prices.some((price) => price === null || price < 0 || price > 30000)) return null;
    values.set(customer, prices);
  }
  // 一部店舗は「学生」の1列だけで、学生会員と学生を分けていない。
  // その場合は同じ学生料金を両区分に割り当てる。
  if (values.has('学生') && !values.has('学生会員')) values.set('学生会員', values.get('学生'));
  if (values.has('学生会員') && !values.has('学生')) values.set('学生', values.get('学生会員'));
  if (CUSTOMERS.some((customer) => !values.has(customer))) return null;
  const rows = columns.map((_, index) => ({
    day: mapped.labels[index],
    prices: Object.fromEntries(CUSTOMERS.map((customer) => [customer, values.get(customer)[index]])),
  }));
  if (mapped.partial) rows.push({ day: '平日', prices: {} });
  if (mapped.holidayUnknown) rows.unshift({ day: '祝', prices: {} });
  return rows;
}

function planFrom(section, { slot = false } = {}) {
  const timeText = norm(section.time).replace(/最終受付\d{1,2}:\d{2}/g, '');
  const time = parseTimeRange(timeText);
  const days = readDays(section);
  if (!time || !days) return null;
  const title = norm(section.header[0]);
  const name = slot ? '30分' : title;
  if (!name || (!slot && !/(?:フリータイム|\d+時間パック)/.test(name))) return null;
  const lastEntry = /最終受付(\d{1,2}:\d{2})/.exec(norm(`${section.time}${section.note}`));
  return {
    plan: {
      name,
      hours: `${time.start === 'OPEN' ? '開店' : time.start}～${time.end === 'CLOSE' ? '閉店' : time.end}`,
      last_entry: lastEntry?.[1] ?? '',
      days,
      drink: '飲み放題込み',
    },
    days,
  };
}

function soloPlans(plans, notes) {
  const note = norm(notes);
  const surcharge = /土[・]?日[・]?祝[^。]{0,40}(?:1名|1人)[^。]{0,40}30分毎\+(\d+)円/.exec(note);
  if (!surcharge) return [];
  const extra = Number(surcharge[1]);
  const solo = [];
  for (const plan of plans) {
    const days = [];
    for (const row of plan.days) {
      if (!Object.keys(row.prices).length) {
        days.push(row);
        continue;
      }
      if (row.day === '平日') {
        days.push(row);
        continue;
      }
      const tokens = weekDays(row.day);
      if (!tokens) return [];
      const marked = [...tokens].filter((day) => ['土', '日', '祝'].includes(day));
      const unmarked = [...tokens].filter((day) => !['土', '日', '祝'].includes(day));
      const labelFor = (values) => DAY_ORDER.filter((day) => values.includes(day)).join('');
      if (unmarked.length) days.push({ day: labelFor(unmarked), prices: row.prices });
      if (marked.length) {
        days.push({
          day: labelFor(marked),
          prices: Object.fromEntries(Object.entries(row.prices).map(([customer, price]) => [customer, price + extra])),
        });
      }
    }
    solo.push({ ...plan, days });
  }
  return solo.length ? [{ band: '30分', customers: CUSTOMERS, plans: solo, note: '' }] : [];
}

export function convertMegabig(sections, hoursText) {
  const week = parseMegabigHours(hoursText);
  if (!week) return { reason: 'no-hours' };
  const slotSections = (sections ?? []).filter((section) => /30分/.test(norm(section.header?.[0] ?? '')));
  if (!slotSections.length) return { reason: 'no-slot-table' };
  const slotPlans = slotSections.map((section) => planFrom(section, { slot: true })).filter(Boolean).map((x) => x.plan);
  if (!slotPlans.length) return { reason: 'unreadable-slot-table' };
  const fixedSections = (sections ?? []).filter((section) =>
    /フリータイム|\d+時間パック/.test(norm(section.header?.[0] ?? '')));
  const fixedPlans = fixedSections.map((section) => planFrom(section)).filter(Boolean).map((x) => x.plan);
  const notes = (sections ?? []).map((section) => section.note ?? '').join(' ');
  const regular = [{ band: '30分', customers: CUSTOMERS, plans: slotPlans, note: '' }];
  if (fixedPlans.length) regular.push({ band: 'フリータイム', customers: CUSTOMERS, plans: fixedPlans, note: '' });
  const alerts = [...new Set(notes.match(/特別料金|特別期間|年末年始|お盆|GW|ゴールデンウィーク|繁忙/g) ?? [])];
  return { regular, solo: soloPlans(slotPlans, notes), week, alerts };
}
