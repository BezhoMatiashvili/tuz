// Numbers, money and dates in the interface language. Times are shown in shop time
// (Asia/Tbilisi), the same day boundaries the server uses for its daily figures.
//
// Both languages are formatted here by hand, with month names and time words from the
// string tables: Chrome ships without Georgian locale data, so Intl would quietly fall
// back to English there while Safari would not.

import { getLang, t } from './strings.js';

const SHOP_ZONE = 'Asia/Tbilisi';
const NBSP = String.fromCharCode(160);

const isKa = () => getLang() === 'ka';

/** A finite number from an API value (the server sends decimals as strings), else null. */
export function toNumber(value) {
  if (value === null || value === undefined || value === '') {
    return null;
  }
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

/** Digits grouped in threes: 65 812,5 in Georgian, 65,812.5 in English. */
function grouped(number, maxDigits, minDigits = 0) {
  const negative = number < 0;
  const fixed = Math.abs(number).toFixed(maxDigits);
  let [whole, fraction = ''] = fixed.split('.');
  while (fraction.length > minDigits && fraction.endsWith('0')) {
    fraction = fraction.slice(0, -1);
  }
  whole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, isKa() ? NBSP : ',');
  const text = fraction ? `${whole}${isKa() ? ',' : '.'}${fraction}` : whole;
  return negative && Number(fixed) !== 0 ? `-${text}` : text;
}

export function num(value, digits = 0) {
  const number = toNumber(value);
  return number === null ? '' : grouped(number, digits);
}

/** Quantities can be fractional (litres); whole numbers stay whole. */
export function qty(value) {
  return num(value, 1);
}

/** Compact figures for axes and line ends: 12.9K / 4.6M, or 12,9 ათ. / 4,6 მლნ. */
export function compact(value) {
  const number = toNumber(value);
  if (number === null) {
    return '';
  }
  const size = Math.abs(number);
  if (size >= 1e6) {
    return t('number.million', { value: grouped(number / 1e6, 1) }).replace(' ', NBSP);
  }
  if (size >= 1e4) {
    return t('number.thousand', { value: grouped(number / 1e3, 1) }).replace(' ', NBSP);
  }
  return grouped(number, size < 100 ? 1 : 0);
}

export function percent(fraction, digits = 1) {
  if (!Number.isFinite(fraction)) {
    return '';
  }
  return `${grouped(fraction * 100, digits)}%`;
}

/** Lari amount. Georgian puts the sign after the number, English before it. */
export function money(value, digits = 0) {
  const number = toNumber(value);
  if (number === null) {
    return '';
  }
  // Tetri appear only when there are any: 150 stays 150, 49.9 becomes 49.90.
  const keep = digits > 0 && Math.abs(number * 100 - Math.round(number) * 100) > 0.5 ? digits : 0;
  const text = grouped(number, digits, keep);
  return isKa() ? `${text}${NBSP}₾` : `₾${text}`;
}

/** Item prices: whole lari stay whole, tetri show when present. */
export function price(value) {
  return money(value, 2);
}

function asDate(value) {
  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value;
  }
  if (typeof value !== 'string' || !value) {
    return null;
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

const partsFormat = new Intl.DateTimeFormat('en-GB', {
  timeZone: SHOP_ZONE, year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit',
  minute: '2-digit', hourCycle: 'h23',
});

/** Calendar fields of a moment, in shop time. */
function shopParts(date) {
  const fields = {};
  for (const part of partsFormat.formatToParts(date)) {
    fields[part.type] = part.value;
  }
  return {
    year: Number(fields.year), month: Number(fields.month), day: Number(fields.day),
    time: `${fields.hour}:${fields.minute}`,
  };
}

function monthName(month, long) {
  return t(long ? 'date.months' : 'date.months_short').split(',')[month - 1] || '';
}

/** 10 Nov 2026, 00:33 in shop time. */
export function dateTime(value) {
  const date = asDate(value);
  if (!date) {
    return '';
  }
  const parts = shopParts(date);
  return `${parts.day} ${monthName(parts.month, false)} ${parts.year}, ${parts.time}`;
}

/** 10 November 2026 in shop time. */
export function longDate(value) {
  const date = asDate(value);
  if (!date) {
    return '';
  }
  const parts = shopParts(date);
  return `${parts.day} ${monthName(parts.month, true)} ${parts.year}`;
}

/** A calendar day string such as 2026-11-10, shown without shifting time zones. */
export function dayLabel(day, withYear = false) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day || ''));
  if (!match) {
    return String(day || '');
  }
  const text = `${Number(match[3])} ${monthName(Number(match[2]), false)}`;
  return withYear ? `${text} ${match[1]}` : text;
}

/** "2 hours ago" for recent moments; older ones fall back to the date. */
export function relativeTime(value, now = Date.now()) {
  const date = asDate(value);
  if (!date) {
    return '';
  }
  const seconds = Math.max(0, Math.round((now - date.getTime()) / 1000));
  if (seconds > 14 * 86400) {
    return longDate(date);
  }
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(seconds / 3600);
  const days = Math.floor(seconds / 86400);
  if (minutes < 1) {
    return t('time.just_now');
  }
  if (hours < 1) {
    return minutes === 1 ? t('time.minute_ago') : t('time.minutes_ago', { count: minutes });
  }
  if (days < 1) {
    return hours === 1 ? t('time.hour_ago') : t('time.hours_ago', { count: hours });
  }
  return days === 1 ? t('time.yesterday') : t('time.days_ago', { count: days });
}

export function bytes(value) {
  const number = toNumber(value);
  if (number === null) {
    return '';
  }
  if (number >= 1048576) {
    return `${grouped(number / 1048576, 1)} MB`;
  }
  return `${grouped(Math.max(1, Math.round(number / 1024)), 0)} KB`;
}

/** Every calendar day from first to last inclusive, as YYYY-MM-DD strings. */
export function dayRange(first, last) {
  const days = [];
  const start = asDate(`${first}T00:00:00Z`);
  const end = asDate(`${last}T00:00:00Z`);
  if (!start || !end || end < start) {
    return days;
  }
  for (let time = start.getTime(); time <= end.getTime() && days.length < 400; time += 86400000) {
    days.push(new Date(time).toISOString().slice(0, 10));
  }
  return days;
}
