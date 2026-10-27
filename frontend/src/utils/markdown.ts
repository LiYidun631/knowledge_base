// 极简 Markdown 渲染：整体先转义 HTML，再做语法转换，避免把模型输出当 HTML 注入。
// 覆盖问答答案常见语法：**加粗**、`行内代码`、### 标题、无序/有序列表、空行分段、
// 图片、表格（标准分隔行格式，以及后端管道行格式——无分隔行、首行即表头）。

export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function renderInline(text: string): string {
  const escaped = escapeHtml(text)
  // 行内代码先转换，避免加粗规则误伤代码内容。
  const withCode = escaped.replace(/`([^`]+)`/g, '<code>$1</code>')
  // 图片语法：src 只会是前端生成的 blob: URL 或后端返回的资产地址，均先经过整体转义。
  const withImages = withCode.replace(
    /!\[([^\]]*)\]\(([^)]+)\)/g,
    '<img class="md-image" src="$2" alt="$1" loading="lazy">',
  )
  return withImages.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
}

function splitTableCells(line: string): string[] | null {
  const trimmed = line.trim()
  if (!trimmed.includes('|')) return null
  const cells = trimmed
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim())
  return cells.length >= 2 ? cells : null
}

function isSeparatorCell(cell: string): boolean {
  return /^:?-{3,}:?$/.test(cell)
}

function renderTable(rows: string[][]): string {
  const columns = Math.max(...rows.map((row) => row.length))
  const buildRow = (row: string[], tag: 'th' | 'td') =>
    `<tr>${Array.from({ length: columns }, (_, i) => `<${tag}>${renderInline(row[i] ?? '')}</${tag}>`).join('')}</tr>`
  const [head, ...body] = rows
  return `<table><thead>${buildRow(head, 'th')}</thead><tbody>${body.map((row) => buildRow(row, 'td')).join('')}</tbody></table>`
}

export function renderMarkdown(source: string, headingShift = 2): string {
  const lines = source.replace(/\r\n/g, '\n').split('\n')
  const output: string[] = []
  let paragraph: string[] = []
  let list: { ordered: boolean; items: string[] } | null = null
  let table: string[][] = []

  const flushParagraph = () => {
    if (paragraph.length > 0) {
      output.push(`<p>${paragraph.map(renderInline).join('<br>')}</p>`)
      paragraph = []
    }
  }
  const flushList = () => {
    if (list && list.items.length > 0) {
      const tag = list.ordered ? 'ol' : 'ul'
      output.push(`<${tag}>${list.items.map((item) => `<li>${renderInline(item)}</li>`).join('')}</${tag}>`)
      list = null
    }
  }
  const flushTable = () => {
    if (table.length >= 2) {
      // 第二行是 |---|---| 分隔行则为标准 markdown 表格，否则按管道行格式（首行即表头）。
      const separator = table[1].every(isSeparatorCell)
      const rows = separator ? [table[0], ...table.slice(2)] : table
      if (rows.length >= 2) output.push(renderTable(rows))
      else output.push(`<p>${table.map((row) => renderInline(row.join(' | '))).join('<br>')}</p>`)
    } else if (table.length === 1) {
      output.push(`<p>${renderInline(table[0].join(' | '))}</p>`)
    }
    table = []
  }

  for (const raw of lines) {
    const line = raw.trimEnd()
    if (line.trim() === '') {
      flushTable()
      flushParagraph()
      flushList()
      continue
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/)
    if (heading) {
      flushTable()
      flushParagraph()
      flushList()
      const level = Math.min(6, heading[1].length + headingShift)
      output.push(`<h${level}>${renderInline(heading[2].trim())}</h${level}>`)
      continue
    }
    const bullet = line.match(/^\s*[-*]\s+(.+)$/)
    if (bullet) {
      flushTable()
      flushParagraph()
      if (!list || list.ordered) {
        flushList()
        list = { ordered: false, items: [] }
      }
      list.items.push(bullet[1].trim())
      continue
    }
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/)
    if (ordered) {
      flushTable()
      flushParagraph()
      if (!list || !list.ordered) {
        flushList()
        list = { ordered: true, items: [] }
      }
      list.items.push(ordered[1].trim())
      continue
    }
    const cells = splitTableCells(line)
    if (cells) {
      if (table.length === 0) {
        flushParagraph()
        flushList()
      }
      table.push(cells)
      continue
    }
    flushTable()
    flushList()
    paragraph.push(line.trim())
  }
  flushTable()
  flushParagraph()
  flushList()
  return output.join('\n')
}
