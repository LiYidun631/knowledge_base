import { client } from '../api/client'

// 答案里的图片走资产接口需要 Bearer 鉴权头，<img> 标签带不了，
// 统一由 axios 拉成 blob URL；同一资产只拉一次，会话内缓存。
const imageCache = new Map<string, Promise<string>>()

function imageBlobUrl(url: string): Promise<string> {
  let pending = imageCache.get(url)
  if (!pending) {
    pending = client
      .get<Blob>(url, { responseType: 'blob' })
      .then(({ data }) => URL.createObjectURL(data))
      .catch(() => '')
    imageCache.set(url, pending)
  }
  return pending
}

// 把内容里的 ![](资产URL) 换成已带鉴权的 blob URL，失败时替换为占位文字。
export async function resolveInlineImages(content: string): Promise<string> {
  const urls = [...content.matchAll(/!\[[^\]]*\]\(([^)]+)\)/g)].map((m) => m[1])
  if (urls.length === 0) return content
  const blobs = new Map<string, string>()
  await Promise.all(
    [...new Set(urls)].map(async (url) => {
      blobs.set(url, await imageBlobUrl(url))
    }),
  )
  return content.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_whole, alt, url) => {
    const src = blobs.get(url)
    if (!src) return '（图片加载失败）'
    return `![${alt}](${src})`
  })
}
