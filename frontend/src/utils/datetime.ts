// 后端与数据库统一按 UTC 存取，界面展示统一换算为北京时间（UTC+8）。
const BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000

function parseUtc(value: string): Date {
  // 不带时区后缀的时间串按 UTC 解析，避免浏览器把它当本地时间。
  const hasOffset = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)
  return new Date(hasOffset ? value : `${value}Z`)
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

function shifted(value: string): Date {
  return new Date(parseUtc(value).getTime() + BEIJING_OFFSET_MS)
}

/** UTC 时间戳 → 北京时间 YYYY-MM-DD HH:mm(:ss)。 */
export function formatBeijingDateTime(value: string, withSeconds = true): string {
  const date = shifted(value)
  if (Number.isNaN(date.getTime())) return value
  const minute = `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`
  return withSeconds ? `${minute}:${pad(date.getUTCSeconds())}` : minute
}

/** UTC 时间戳 → 北京时间 YYYY-MM-DD。 */
export function formatBeijingDate(value: string): string {
  const date = shifted(value)
  if (Number.isNaN(date.getTime())) return value
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`
}

/** UTC 时间戳 → 北京时间 HH:mm(:ss)。 */
export function formatBeijingClock(value: string, withSeconds = false): string {
  const date = shifted(value)
  if (Number.isNaN(date.getTime())) return value
  const minute = `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`
  return withSeconds ? `${minute}:${pad(date.getUTCSeconds())}` : minute
}

/** 北京时间日期键 YYYY-MM-DD，用于按日分组。 */
export function beijingDateKey(value: string): string {
  return formatBeijingDate(value)
}

/** 北京时间「今天」的日期键。 */
export function todayBeijingDateKey(): string {
  return new Date(Date.now() + BEIJING_OFFSET_MS).toISOString().slice(0, 10)
}
